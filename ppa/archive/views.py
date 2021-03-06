from collections import OrderedDict
import csv
from json.decoder import JSONDecodeError
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.core.paginator import Paginator
from django.http import HttpResponse, Http404
from django.shortcuts import redirect, get_object_or_404, render
from django.utils.http import urlencode
from django.utils.timezone import now
from django.urls import reverse
from django.views.generic import ListView, DetailView
from django.views.generic.base import RedirectView, TemplateView
from django.views.generic.edit import FormView
from SolrClient.exceptions import SolrError

from ppa.archive.forms import SearchForm, AddToCollectionForm, \
    SearchWithinWorkForm, AddFromHathiForm
from ppa.archive.models import DigitizedWork, NO_COLLECTION_LABEL
from ppa.archive.solr import get_solr_connection, PagedSolrQuery
from ppa.common.views import AjaxTemplateMixin, LastModifiedMixin, \
    LastModifiedListMixin
from ppa.archive.util import HathiImporter


logger = logging.getLogger(__name__)


class DigitizedWorkListView(AjaxTemplateMixin, LastModifiedListMixin, ListView):
    '''Search and browse digitized works.  Based on Solr index
    of works and pages.'''

    model = DigitizedWork
    # FIXME should auto-determine this
    template_name = 'archive/digitizedwork_list.html'
    ajax_template_name = 'archive/snippets/results_list.html'
    form_class = SearchForm
    paginate_by = 50
    #: title for metadata / preview
    meta_title = 'Princeton Prosody Archive'
    #: page description for metadata/preview
    meta_description = '''The Princeton Prosody Archive is a full-text
    searchable database of thousands of historical documents about the
    study of language and the study of poetry.'''

    # keyword query; assume no search terms unless set
    query = None

    # search title query field syntax
    search_title_query = '{!type=edismax qf=$search_title_qf pf=$search_title_pf v=$title_query}'

    def get_queryset(self, **kwargs):
        form_opts = self.request.GET.copy()
        # if relevance sort is requested but there is no keyword search
        # term present, clear it out and fallback to default sort
        if not self.form_class().has_keyword_query(form_opts):
            if 'sort' in form_opts and form_opts['sort'] == 'relevance':
                del form_opts['sort']

        searchform_defaults = self.form_class.defaults()

        for key, val in searchform_defaults.items():
            # set as list to avoid nested lists
            # follows solution using in derrida-django for InstanceListView
            if isinstance(val, list):
                form_opts.setlistdefault(key, val)
            else:
                form_opts.setdefault(key, val)

        # NOTE: Default sort for keyword search should be relevance but
        # currently no way to distinguish default sort from user selected
        self.form = self.form_class(form_opts)

        # if the form is not valid, return an empty queryset and bail out
        # (queryset needed for django paginator)
        if not self.form.is_valid():
            return DigitizedWork.objects.none()

        work_query = sort = keyword_query = None
        # components of query to filter digitized works
        if self.form.is_valid():
            search_opts = self.form.cleaned_data
            self.query = search_opts.get("query", None)
            sort = search_opts.get("sort", None)
            collections = search_opts.get("collections", None)

            if self.query:
                # simple keyword search across all configured fields
                # group to ensure boolean logic applies to all terms
                keyword_query = "(%s)" % self.query

            work_q = []

            # restrict by collection
            if collections:
                # if *all* collections are selected, no need to filter
                # (will return everything either way; keep the query simpler)
                if len(collections) != len(self.form.fields['collections'].choices):
                    work_q.append('collections_exact:(%s)' % \
                        (' OR '.join(['"%s"' % coll for coll in collections])))

            # For collection exclusion logic to work properly, if no
            # collections are selected, no items should be returned.
            # This query should return no items but still provide facet
            # data to populate the collection filters on the form properly.
            else:
                work_q.append('item_type:work AND -collections_exact:[* TO *]')

            # filter books by title or author if there is are search terms
            title_query = search_opts.get('title', None)
            if title_query:
                # special syntax to use query field configured in solr conf
                # to search title and subtitle, with boosting
                work_q.append(self.search_title_query)

            author_query = search_opts.get('author', None)
            if author_query:
                work_q.append('author:(%s)' % author_query)

        range_opts = {
            'facet.range': self.form.range_facets
        }
        for range_facet in self.form.range_facets:
            # range filter requested in search options
            start = end = None
            # if start or end is specified on the form, add a filter query
            if range_facet in search_opts and search_opts[range_facet]:
                start, end = search_opts[range_facet].split('-')
                range_filter = '[%s TO %s]' % (start or '*', end or '*')
                # find works restricted by range
                work_q.append('%s:%s' % (range_facet, range_filter))

            # get minimum and maximum pub date values from the db
            pubmin, pubmax = self.form.pub_date_minmax()

            # NOTE: hard-coded values are fallback logic for when
            # no contents are in the database and pubmin/pubmax are None
            start = int(start) if start else pubmin or 0
            end = int(end) if end else pubmax or 1922

            # Configure range facet options specific to current field, to
            # support more than one range facet (even though not currently needed)
            range_opts.update({
                # current range filter
                'f.%s.facet.range.start' % range_facet: start,
                # NOTE: per facet.range.include documentation, default behavior
                # is to include lower bound and exclude upper bound.
                # For simplicity, increase range end by one.
                'f.%s.facet.range.end' % range_facet: end + 1,
                # calculate gap based start and end & desired number of slices
                # ideally, generate 24 slices; minimum gap size of 1
                'f.%s.facet.range.gap' % range_facet: max(1, int((end - start) / 24)),
                # restrict last range to *actual* maximum value
                'f.%s.facet.range.hardend' % range_facet: True,
            })

        # if there are any queries to filter works  or search by text,
        # combine the queries and construct solr query
        if work_q or keyword_query:
            query_parts = []

            # work-level metadata queries and filters
            if work_q:
                # combine all work filters from search form input
                # (input from different fields are combined via *AND*)
                work_query = '(%s)' % ' AND '.join(work_q)
                # NOTE: grouping required for work_query to work properly
                # on the join search for works that match the filters OR
                # for pages that belong to a work that matches, but only
                # if there is also a keyword_query. If there is not
                # keyword_query, pages are not needed.
                if keyword_query:
                    query_parts.append(
                        '(%s OR {!join from=id to=source_id v=$work_query})' % work_query
                    )
                else:
                    query_parts.append(work_query)

            # general text query, if there is one
            if keyword_query:
                # search for works that match the filter OR for works
                # associated with pages that match
                query_parts.append(
                    '(%s OR {!join from=source_id to=id v=$keyword_query})' % keyword_query
                )

            # combine work and text queries together with AND
            solr_q = ' AND '.join(query_parts)
            logger.debug("Solr search query: %s", solr_q)
        else:
            # if no work queries or filters are specified,
            # return all works (no pages needed)
            solr_q = 'item_type:work'

        solr_sort = self.form.get_solr_sort_field(sort)
        fields = '*,score'
        # NOTE: For now, defaulting to always including score in fields

        # use filter query to collapse works and pages into groups
        # sort so work is first, then by page order
        collapse_q = '{!collapse field=source_id sort="order asc"}'

        # basic solr options, including filter query
        solr_opts = {
            'q': solr_q,
            'sort': solr_sort,
            'fl': fields,
            'fq': collapse_q,
            # turn on faceting and add any self.form facet_fields
            'facet': 'true',
            'facet.field': [field for field in self.form.facet_fields],
            # sort by alpha on facet label rather than count
            'facet.sort': 'index',
            # default expand sort is score desc
            'expand': 'true',
            'expand.rows': 2,   # number of items in the collapsed group, i.e pages to display
            # explicitly query pages on text content (join q seems to skip qf)
            'keyword_query': 'content:%s' % keyword_query,
            'title_query': title_query,
            'work_query': work_query
        }

        # add facet range options to the solr options
        solr_opts.update(range_opts)
        self.solrq = PagedSolrQuery(solr_opts)

        return self.solrq

    def get_page_highlights(self, page_groups):
        '''If there is a keyword search, query Solr for matching pages
        with text highlighting.  Note that this has to be done as
        a separate query because Solr doesn't support highlighting on
        collapsed items.'''

        page_highlights = {}
        if not self.query or not page_groups:
            # if there is no keyword query, bail out
            return page_highlights

        # generate a list of page ids from the grouped results
        page_ids = [page['id'] for results in page_groups.values()
                    for page in results['docs']]

        if not page_ids:
            # if no page ids were found, bail out
            return page_highlights

        # Query solr for the desired pages by id with the same
        # keyword search.
        # NOTE: This id query assumes OR is default; if  that changes,
        # add an explicitly OR here between ids.
        # NOTE 2: using quotes around ids to handle ids that include
        # colons, e.g. ark:/foo/bar .
        solr_pageq = PagedSolrQuery({
            'q': 'content:(%s) AND id:(%s)' % \
                (self.query, ' '.join('"%s"' % pid for pid in page_ids)),
            # enable highlighting on content field with 3 snippets
            'hl': True,
            'hl.fl': 'content',
            'hl.snippets': 3,
            # use Unified Highlighter (not default but recommended)
            'hl.method': 'unified',
            # override solr default of 10 results to return all pages
            'rows': len(page_ids),
        })
        return solr_pageq.get_highlighting()

    def get_context_data(self, **kwargs):
        # if the form is not valid, bail out
        if not self.form.is_valid():
            context = super().get_context_data(**kwargs)
            context['search_form'] = self.form
            return context

        page_groups = facet_ranges = None
        try:
            # catch an error querying solr when the search terms cannot be parsed
            # (e.g., incomplete exact phrase)
            context = super().get_context_data(**kwargs)
            page_groups = self.solrq.get_expanded()
            facet_dict = self.solrq.get_facets()

            self.form.set_choices_from_facets(facet_dict)
            # needs to be inside try/catch or it will re-trigger any error
            facet_ranges = self.solrq.facet_ranges
            # facet ranges are used for display; when sending to solr we
            # increase the end bound by one so that year is included;
            # subtract it back so display matches user entered dates
            facet_ranges['pub_date']['end'] -= 1

        except SolrError as solr_err:
            # override object list with an empty list that can be paginated
            # so that template display will still work properly
            self.object_list = DigitizedWork.objects.none()
            context = super().get_context_data(**kwargs)
            if 'Cannot parse' in str(solr_err):
                error_msg = 'Unable to parse search query; please revise and try again.'
            else:
                # NOTE: this error should possibly be raised; 500 error?
                # or set status on the response?
                error_msg = 'Something went wrong.'
            context['error'] = error_msg

        context.update({
            'search_form': self.form,
            # total and object_list provided by paginator
            'page_groups': page_groups,
            # range facet data for publication date
            'facet_ranges': facet_ranges,
            'page_highlights': self.get_page_highlights(page_groups),
            # query for use template links to detail view with search
            'query': self.query,
            'NO_COLLECTION_LABEL': NO_COLLECTION_LABEL,
            'page_title': self.meta_title,
            'page_description': self.meta_description
        })
        return context

    def last_modified(self):
        '''override last modified logic to work with Solr'''

        # override sort to return most recent modification date,
        # only return last modified value and nothing else

        # use most recent modification time of anything in the index,
        # since any search on the archive page coud change based
        # on work or text content changing in the index, (could add
        # or remove results from any particular set)
        query_opts = {
            'q': '*:*',
            'sort': 'last_modified desc',
            'fl': 'last_modified'
        }

        # if a syntax or other solr error happens, no date to return
        try:
            psq = PagedSolrQuery(query_opts)
            # Solr stores date in isoformat; convert to datetime
            return self.solr_timestamp_to_datetime(psq[0]['last_modified'])
            # skip extra call to Solr to check count and just grab the first
            # item if it exists
        except (IndexError, KeyError, SolrError):
            pass


class DigitizedWorkDetailView(AjaxTemplateMixin, LastModifiedMixin, DetailView):
    '''Display details for a single digitized work. If a work has been
    surpressed, returns a 410 Gone response.'''
    ajax_template_name = 'archive/snippets/results_within_list.html'
    model = DigitizedWork
    slug_field = 'source_id'
    slug_url_kwarg = 'source_id'
    form_class = SearchWithinWorkForm
    paginate_by = 50

    def get_template_names(self):
        if self.object.status == DigitizedWork.SUPPRESSED:
            return '410.html'
        return super().get_template_names()

    def last_modified(self):
        """get last index modification from Solr, as it will be more
        current than object last modified."""

        # if there is a solr error or last modified is not avilable,
        # skip last-modified behavior and display page
        try:
            psq = PagedSolrQuery({
                'q': 'source_id:"%s"' % self.object.source_id,
                'sort': 'last_modified desc',
                'fl': 'last_modified'
            })
            # Solr stores date in isoformat; convert to datetime
            return self.solr_timestamp_to_datetime(psq[0]['last_modified'])
            # skip extra call to Solr to check count and just grab the first
            # item if it exists
        except (SolrError, IndexError, KeyError):
            pass

    def get(self, *args, **kwargs):
        response = super().get(*args, **kwargs)
        # set status code to 410 gone for suppressed works
        if self.object.is_suppressed:
            response.status_code = 410
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        digwork = context['object']
        # if suppressed, don't do any further processing
        if digwork.is_suppressed:
            return context

        context.update({
            'page_title': digwork.title,
            'page_description': digwork.public_notes
        })

        # pull in the query if it exists to use
        query = self.request.GET.get('query', '')
        form_opts = self.request.GET.copy()
        form = self.form_class(form_opts)
        context['search_form'] = form
        solr_pageq = None

        # search within a volume currently only supported for hathi content
        if query and digwork.source == DigitizedWork.HATHI:
            context['query'] = query
            solr_q = query
            solr_opts = {
                'q': 'content:(%s)' % solr_q,
                # sort by page order by default
                'sort': 'order asc',
                # 'fl': '*',
                'fl': 'id,source_id,order,title,label',  # Limiting only to needed fields
                'fq': 'source_id:("%s") AND item_type:page' % digwork.source_id,
                # configure highlighting on page text content
                'hl': True,
                'hl.fl': 'content',
                'hl.snippets': 3,
                # not default but recommended
                'hl.method': 'unified',
            }
            logger.info("Solr page keyword search query: %s" % solr_q)

            try:
                # configure paginator and set in context
                solr_pageq = PagedSolrQuery(solr_opts)
                paginator = Paginator(solr_pageq, per_page=self.paginate_by)
                page = self.request.GET.get('page', 1)
                context.update({
                    'search_form': form,
                    'page_obj': paginator.page(page),
                    # get matching pages and highlights and set in context
                    # 'page_highlights': solr_pageq.get_highlighting() if solr_pageq else {}
                    'page_highlights': solr_pageq.get_highlighting() if solr_pageq else {},
                    'solr_results': solr_pageq.get_results() if solr_pageq else []
                })

            except SolrError as solr_err:
                if 'Cannot parse' in str(solr_err):
                    error_msg = ('Unable to parse search query; '
                                 'please revise and try again.')
                else:
                    # NOTE: this error should possibly be raised; 500 error?
                    error_msg = 'Something went wrong.'
                context['error'] = error_msg

        return context


class DigitizedWorkByRecordId(RedirectView):
    '''Redirect from DigitizedWork record id to detail view when possible.
    If there is only one record found, redirect. If multiple are found, 404.'''
    permanent = False
    query_string = False

    def get_redirect_url(self, *args, **kwargs):
        try:
            work = get_object_or_404(DigitizedWork, record_id=kwargs['record_id'])
            return work.get_absolute_url()
        except MultipleObjectsReturned:
            raise Http404


class DigitizedWorkCSV(ListView):
    '''Export of digitized work details as CSV download.'''
    # NOTE: csv logic could be extracted as a view mixin for reuse
    model = DigitizedWork
    # order by id for now, for simplicity
    ordering = 'id'
    header_row = [
        'Database ID', 'Source ID', 'Record ID', 'Title', 'Subtitle',
        'Sort title', 'Author', 'Publication Date', 'Publication Place',
        'Publisher', 'Enumcron', 'Collection', 'Public Notes', 'Notes',
        'Page Count', 'Status', 'Date Added', 'Last Updated'
    ]

    def get_csv_filename(self):
        '''Return the CSV file name based on the current datetime.

        :returns: the filename for the CSV to be generated
        :rtype: str
        '''
        return 'ppa-digitizedworks-%s.csv' % now().strftime('%Y%m%dT%H:%M:%S')

    def get_data(self):
        '''Get data for the CSV.

        :returns: rows for CSV columns
        :rtype: tuple
        '''
        return ((dw.id, dw.source_id, dw.record_id, dw.title, dw.subtitle,
                 dw.sort_title, dw.author, dw.pub_date, dw.pub_place,
                 dw.publisher, dw.enumcron,
                 ';'.join([coll.name for coll in dw.collections.all()]),
                 dw.public_notes, dw.notes, dw.page_count,
                 dw.get_status_display(), dw.added, dw.updated
                 )
                for dw in self.get_queryset().prefetch_related('collections'))
        # NOTE: prefetch collections so they are retrieved more efficiently
        # all at once, rather than one at a time for each item

    def render_to_csv(self, data):
        '''
        Render the CSV as an HTTP response.

        :rtype: :class:`django.http.HttpResponse`
        '''
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="%s"' % \
            self.get_csv_filename()

        writer = csv.writer(response)
        writer.writerow(self.header_row)
        for row in data:
            writer.writerow(row)
        return response

    def get(self, *args, **kwargs):
        '''Return CSV file on GET request.'''
        return self.render_to_csv(self.get_data())


class AddToCollection(ListView, FormView):
    '''
    View to bulk add a queryset of :class:`ppa.archive.models.DigitizedWork`
    to a set of :class:`ppa.archive.models.Collection instances`.

    Restricted to staff users via staff_member_required on url.
    '''

    model = DigitizedWork
    template_name = 'archive/add_to_collection.html'
    form_class = AddToCollectionForm

    def get_success_url(self):
        '''
        Redirect to the :class:`ppa.archive.models.DigitizedWork`
        change_list in the Django admin with pagination and filters preserved.
        Expects :meth:`ppa.archive.admin.bulk_add_collection`
        to have set 'collection-add-filters' as a dict in the request's
        session.
        '''
        change_list = reverse('admin:archive_digitizedwork_changelist')
        # get request.session's querystring filter, and if it exists
        # use it to set the querystring
        querystring = ''
        filter_dict = self.request.session.get('collection-add-filters', None)
        if filter_dict:
            querystring = '?%s' % urlencode(filter_dict)
        return '%s%s' % (change_list, querystring)

    def get_queryset(self, *args, **kwargs):
        '''Return a queryset filtered by id, or empty list if no ids'''
        # get ids from session if there are any
        ids = self.request.session.get('collection-add-ids', [])
        # if somehow a problematic non-pk is pushed, will be ignored in filter
        digworks = DigitizedWork.objects.filter(id__in=ids
                                                if ids else []).order_by('id')
        # revise the stored list in session to eliminate any pks
        # that don't exist
        self.request.session['collection-add-ids'] = \
            list(digworks.values_list('id', flat=True))
        return digworks

    def post(self, request, *args, **kwargs):
        '''
        Add :class:`ppa.archive.models.DigitizedWork` instances passed in form
        data to selected instances of :class:`ppa.archive.models.Collection`,
        then return to change_list view.

        Expects a list of DigitizedWork ids to be set in the request session.

        '''
        form = AddToCollectionForm(request.POST)
        if form.is_valid() and request.session['collection-add-ids']:
            data = form.cleaned_data
            # get digitzed works from validated form
            digitized_works = self.get_queryset()
            del request.session['collection-add-ids']
            for collection in data['collections']:
                # add rather than set to ensure add does not replace
                # previous digitized works in set.
                collection.digitizedwork_set.add(*digitized_works)
            # reindex solr with the new collection data
            solr_docs = [work.index_data() for work in digitized_works]
            solr, solr_collection = get_solr_connection()
            solr.index(solr_collection, solr_docs,
                       params={'commitWithin': 2000})
            # create a success message to add to message framework stating
            # what happened
            num_works = digitized_works.count()
            collections = ', '.join(collection.name for
                                    collection in data['collections'])
            messages.success(request, 'Successfully added %d works to: %s.'
                             % (num_works, collections))
            # redirect to the change list with the message intact
            return redirect(self.get_success_url())
        # make form error more descriptive, default to an error re: pks
        if 'collections' in form.errors:
            del form.errors['collections']
            form.add_error(
                'collections',
                ValidationError('Please select at least one Collection')
            )
        # Provide an object list for ListView and emulate CBV calling
        # render_to_response to pass form with errors; just calling super
        # doesn't pass the form with error set
        self.object_list = self.get_queryset()
        return self.render_to_response(self.get_context_data(form=form))


class AddFromHathiView(FormView):
    '''Admin view to add new HathiTrust records by providing a list
    of ids.'''
    template_name = 'archive/add_from_hathi.html'
    form_class = AddFromHathiForm
    page_title = 'Add new records from HathiTrust'

    def get_context_data(self, *args, **kwargs):
        # Add page title to template context data
        context = super().get_context_data(*args, **kwargs)
        context['page_title'] = self.page_title
        return context

    def form_valid(self, form):
        # Process valid form data; should return an HttpResponse.

        # get list of ids from form input
        htids = form.get_hathi_ids()

        htimporter = HathiImporter(htids)
        htimporter.filter_existing_ids()
        # add items, and create log entries associated with current user
        htimporter.add_items(log_msg_src='via django admin',
                             user=self.request.user)
        htimporter.index()

        # generate lookup for admin urls keyed on source id to simplify
        # template logic needed
        admin_urls = {htid: reverse('admin:archive_digitizedwork_change', args=[pk])
                      for htid, pk in htimporter.existing_ids.items()}
        for work in htimporter.imported_works:
            admin_urls[work.source_id] = \
                reverse('admin:archive_digitizedwork_change', args=[work.pk])

        # Default form_valid behavior is to redirect to success url,
        # but we actually want to redisplay the template with results
        # and allow submitting the form again with a new batch.
        return render(self.request, self.template_name, context={
            'results': htimporter.output_results(),
            'existing_ids': htimporter.existing_ids,
            'form': self.form_class(),  # new form instance
            'page_title': self.page_title,
            'admin_urls': admin_urls
            })


class OpenSearchDescriptionView(TemplateView):
    '''Basic open search description for searching the archive
    via browser or other tools.'''

    template_name = "archive/opensearch_description.xml"
    content_type = 'application/opensearchdescription+xml'
