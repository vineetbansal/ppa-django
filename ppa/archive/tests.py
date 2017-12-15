from datetime import date
from io import StringIO
import json
import os.path
from unittest.mock import patch, Mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
import pymarc
import pytest
import requests
from SolrClient import SolrClient

from ppa.archive import hathi
# from ppa.archive .hathi import HathiBibliographicRecord, HathiBibliographicAPI,
from ppa.archive.models import DigitizedWork
from ppa.archive.solr import get_solr_connection, SolrSchema, CoreAdmin, \
    PagedSolrQuery


FIXTURES_PATH = os.path.join(settings.BASE_DIR, 'ppa', 'archive', 'fixtures')


@patch('ppa.archive.hathi.requests')
class TestHathiBibliographicAPI(TestCase):
    bibdata = os.path.join(FIXTURES_PATH,
        'bibdata_brief_njp.32101013082597.json')

    def test_brief_record(self, mockrequests):
        mockrequests.codes = requests.codes
        mockrequests.get.return_value.status_code = requests.codes.ok

        bib_api = hathi.HathiBibliographicAPI()
        htid = 'njp.32101013082597'

        # no result found
        mockrequests.get.return_value.json.return_value = {}
        with pytest.raises(hathi.HathiItemNotFound):
            bib_api.brief_record('htid', htid)

        # use fixture to simulate result found
        with open(self.bibdata) as sample_bibdata:
            mockrequests.get.return_value.json.return_value = json.load(sample_bibdata)

        record = bib_api.brief_record('htid', htid)
        assert isinstance(record, hathi.HathiBibliographicRecord)

        # check expected url was called
        mockrequests.get.assert_any_call(
            'http://catalog.hathitrust.org/api/volumes/brief/htid/%s.json' % htid)

        # ark ids are not escaped
        htid = 'aeu.ark:/13960/t1pg22p71'
        bib_api.brief_record('htid', htid)
        mockrequests.get.assert_any_call(
            'http://catalog.hathitrust.org/api/volumes/brief/htid/%s.json' % htid)

        # alternate id
        oclc_id = '424023'
        bib_api.brief_record('oclc', oclc_id)
        mockrequests.get.assert_any_call(
            'http://catalog.hathitrust.org/api/volumes/brief/oclc/%s.json' % oclc_id)

    def test_record(self, mockrequests):
        mockrequests.codes = requests.codes
        mockrequests.get.return_value.status_code = requests.codes.ok

        bib_api = hathi.HathiBibliographicAPI()
        htid = 'njp.32101013082597'

        # use fixture to simulate result found
        with open(self.bibdata) as sample_bibdata:
            mockrequests.get.return_value.json.return_value = json.load(sample_bibdata)

        record = bib_api.record('htid', htid)
        assert isinstance(record, hathi.HathiBibliographicRecord)

        # check expected url was called - full instead of brief
        mockrequests.get.assert_any_call(
            'http://catalog.hathitrust.org/api/volumes/full/htid/%s.json' % htid)


class TestHathiBibliographicRecord(TestCase):
    bibdata_full = os.path.join(FIXTURES_PATH,
        'bibdata_full_njp.32101013082597.json')
    bibdata_brief = os.path.join(FIXTURES_PATH,
        'bibdata_brief_njp.32101013082597.json')

    def setUp(self):
        with open(self.bibdata_full) as bibdata:
            self.record = hathi.HathiBibliographicRecord(json.load(bibdata))

        with open(self.bibdata_brief) as bibdata:
            self.brief_record = hathi.HathiBibliographicRecord(json.load(bibdata))

    def test_properties(self):
        record = self.record
        assert record.record_id == '008883512'
        assert record.title == \
            "Lectures on the literature of the age of Elizabeth, and Characters of Shakespear's plays,"
        assert record.pub_dates == ['1882']
        copy_details = record.copy_details('njp.32101013082597')
        assert isinstance(copy_details, dict)
        assert copy_details['orig'] == 'Princeton University'

        assert record.copy_details('bogus') is None

        # brief record should work the same way
        record = self.brief_record
        assert record.record_id == '008883512'
        assert record.title == \
            "Lectures on the literature of the age of Elizabeth, and Characters of Shakespear's plays,"
        assert record.pub_dates == ['1882']
        copy_details = record.copy_details('njp.32101013082597')
        assert isinstance(copy_details, dict)
        assert copy_details['orig'] == 'Princeton University'

        assert record.copy_details('bogus') is None

    def test_copy_last_updated(self):
        update_date = self.record.copy_last_updated('njp.32101013082597')
        assert isinstance(update_date, date)
        assert update_date == date(2017, 3, 24)

    def test_marcxml(self):
        record = self.record
        assert isinstance(record.marcxml, pymarc.Record)
        assert record.marcxml.author() == 'Hazlitt, William, 1778-1830.'

        # test no marcxml in data, e.g. brief record
        assert self.brief_record.marcxml is None


TEST_SOLR_CONNECTIONS = {
    'default': {
        'COLLECTION': 'testppa',
        'URL': 'http://localhost:191918984/solr/',
        'ADMIN_URL': 'http://localhost:191918984/solr/admin/cores'
    }
}

@override_settings(SOLR_CONNECTIONS=TEST_SOLR_CONNECTIONS)
def test_get_solr_connection():
    # test basic solr connection setup
    solr, collection = get_solr_connection()
    assert isinstance(solr, SolrClient)
    assert solr.host == TEST_SOLR_CONNECTIONS['default']['URL']
    assert collection == TEST_SOLR_CONNECTIONS['default']['COLLECTION']

    # TODO: test error handling once we have some


@override_settings(SOLR_CONNECTIONS=TEST_SOLR_CONNECTIONS)
@patch('ppa.archive.solr.get_solr_connection')
class TestSolrSchema(TestCase):

    def test_solr_schema_fields(self, mock_get_solr_connection):
        mocksolr = Mock()
        mock_get_solr_connection.return_value = (mocksolr, 'testcoll')
        mocksolr.schema.get_schema_fields.return_value = {
            'fields': [
                {'name': 'author', 'type': 'text_en', 'required': False},
                {'name': 'title', 'type': 'text_en', 'required': False},
                {'name': 'date', 'type': 'int', 'required': False},
            ]
        }
        schema = SolrSchema()
        fields = schema.solr_schema_fields()
        for expected_field in ['author', 'title', 'date']:
            assert expected_field in fields

    def test_update_solr_schema(self, mock_get_solr_connection):
        mocksolr = Mock()
        coll = 'testcoll'
        mock_get_solr_connection.return_value = (mocksolr, coll)
        # simulate no fields in solr
        mocksolr.schema.get_schema_fields.return_value = {'fields': []}
        test_copy_field = {'source': 'id', 'dest': 'text'}
        mocksolr.schema.get_schema_copyfields.return_value = [
            test_copy_field
        ]

        schema = SolrSchema()
        schema.fields = [
            {'name': 'author', 'type': 'text_en', 'required': False},
            {'name': 'title', 'type': 'text_en', 'required': False},
            {'name': 'pub_date', 'type': 'int', 'required': False},
        ]

        created, updated, removed = schema.update_solr_schema()
        assert created == 3
        assert updated == 0
        assert removed == 0
        for field_def in schema.fields:
            mocksolr.schema.create_field.assert_any_call(coll, field_def)

        mocksolr.schema.replace_field.assert_not_called()
        mocksolr.schema.delete_field.assert_not_called()
        mocksolr.schema.delete_copy_field.assert_called_with(coll, test_copy_field)

        # simulate all fields in solr
        mocksolr.schema.get_schema_fields.return_value = {'fields': schema.fields}
        mocksolr.schema.create_field.reset_mock()

        created, updated, removed = schema.update_solr_schema()
        assert created == 0
        assert updated == 3
        assert removed == 0
        mocksolr.schema.create_field.assert_not_called()
        for field_def in schema.fields:
            mocksolr.schema.replace_field.assert_any_call(coll, field_def)
        mocksolr.schema.delete_field.assert_not_called()

        # remove outdated fields
        mocksolr.schema.get_schema_fields.return_value = {'fields':
            schema.fields + [
            {'name': '_root_'},
            {'name': '_text_'},
            {'name': '_version_'},
            {'name': 'id'},
            {'name': 'oldfield'},
        ]}
        mocksolr.schema.create_field.reset_mock()
        mocksolr.schema.replace_field.reset_mock()
        created, updated, removed = schema.update_solr_schema()
        assert created == 0
        assert updated == 3
        assert removed == 1
        mocksolr.schema.delete_field.assert_called_once_with(coll, 'oldfield')


@override_settings(SOLR_CONNECTIONS=TEST_SOLR_CONNECTIONS)
class TestCoreAdmin(TestCase):

    def test_init(self):
        core_adm = CoreAdmin()
        assert core_adm.admin_url == settings.SOLR_CONNECTIONS['default']['ADMIN_URL']

    @patch('ppa.archive.solr.requests')
    def test_reload(self, mockrequests):
        # simulate success
        mockrequests.codes = requests.codes
        mockrequests.get.return_value.status_code = requests.codes.ok
        core_adm = CoreAdmin()
        # should return true for success
        assert core_adm.reload()
        # should call with configured collection
        mockrequests.get.assert_called_with(core_adm.admin_url,
            params={'action': 'RELOAD',
                    'core': settings.SOLR_CONNECTIONS['default']['COLLECTION']})

        # reload specific core
        assert core_adm.reload('othercore')
        mockrequests.get.assert_called_with(core_adm.admin_url,
            params={'action': 'RELOAD',
                    'core': 'othercore'})

        # failure
        mockrequests.get.return_value.status_code = requests.codes.not_found
        assert not core_adm.reload('othercore')


class TestDigitizedWork(TestCase):

    fixtures = ['sample_digitized_works']

    def test_str(self):
        digwork = DigitizedWork(source_id='njp.32101013082597')
        assert str(digwork) == digwork.source_id

    def test_populate_from_bibdata(self):
        with open(TestHathiBibliographicRecord.bibdata_full) as bibdata:
            full_bibdata = hathi.HathiBibliographicRecord(json.load(bibdata))

        with open(TestHathiBibliographicRecord.bibdata_brief) as bibdata:
            brief_bibdata = hathi.HathiBibliographicRecord(json.load(bibdata))

        digwork = DigitizedWork(source_id='njp.32101013082597')
        digwork.populate_from_bibdata(brief_bibdata)
        assert digwork.title == brief_bibdata.title
        assert digwork.pub_date == brief_bibdata.pub_dates[0]
        # no enumcron in this record
        assert digwork.enumcron == ''
        # fields from marc not set
        assert not digwork.author
        assert not digwork.pub_place
        assert not digwork.publisher

        # test no pub date
        brief_bibdata.info['publishDates'] = []
        digwork = DigitizedWork(source_id='njp.32101013082597')
        digwork.populate_from_bibdata(brief_bibdata)
        assert not digwork.pub_date

        # TODO: test enumcron from copy details

        # populate from full record
        digwork.populate_from_bibdata(full_bibdata)
        assert digwork.author == full_bibdata.marcxml.author()
        assert digwork.pub_place == full_bibdata.marcxml['260']['a']
        assert digwork.publisher == full_bibdata.marcxml['260']['b']

        # TODO: test publication info unavailable?

    def test_index_data(self):
        digwork = DigitizedWork(source_id='njp.32101013082597',
            title='Structure of English Verse', pub_date=1884,
            author='Charles Witcomb', pub_place='Paris',
            publisher='Mesnil-Dramard',
            source_url='https://hdl.handle.net/2027/njp.32101013082597')
        index_data = digwork.index_data()
        assert index_data['id'] == digwork.source_id
        assert index_data['srcid'] == digwork.source_id
        assert index_data['item_type'] == 'work'
        assert index_data['title'] == digwork.title
        assert index_data['author'] == digwork.author
        assert index_data['pub_place'] == digwork.pub_place
        assert index_data['pub_date'] == digwork.pub_date
        assert index_data['publisher'] == digwork.publisher
        assert index_data['src_url'] == digwork.source_url
        assert not index_data['enumcron']

        # with enumcron
        digwork.enumcron = 'v.7 (1848)'
        assert digwork.index_data()['enumcron'] == digwork.enumcron

    def test_get_absolute_url(self):
        work = DigitizedWork.objects.first()
        assert work.get_absolute_url() == \
            reverse('archive:detail', kwargs={'source_id': work.source_id})


@pytest.fixture
def empty_solr():
    # pytest solr fixture; updates solr schema
    with override_settings(SOLR_CONNECTIONS={'default': settings.SOLR_CONNECTIONS['test']}):
        # reload core before and after to ensure field list is accurate
        CoreAdmin().reload()
        solr_schema = SolrSchema()
        cp_fields = solr_schema.solr.schema.get_schema_copyfields(solr_schema.solr_collection)
        current_fields = solr_schema.solr_schema_fields()

        for cp_field in cp_fields:
            solr_schema.solr.schema.delete_copy_field(solr_schema.solr_collection, cp_field)
        for field in current_fields:
            solr_schema.solr.schema.delete_field(solr_schema.solr_collection, field)
        CoreAdmin().reload()


@pytest.fixture
def solr():
    # pytest solr fixture; updates solr schema
    with override_settings(SOLR_CONNECTIONS={'default': settings.SOLR_CONNECTIONS['test']}):
        # reload core before and after to ensure field list is accurate
        CoreAdmin().reload()
        SolrSchema().update_solr_schema()
        CoreAdmin().reload()


class TestSolrSchemaCommand(TestCase):

    def test_connection_error(self):
        # simulate no solr running
        with override_settings(SOLR_CONNECTIONS=TEST_SOLR_CONNECTIONS):
            with pytest.raises(CommandError):
                call_command('solr_schema')

    @pytest.mark.skip   # skip for now - causing an error on travis-ci
    @pytest.mark.usefixtures("empty_solr")
    def test_empty_solr(self):
        with override_settings(SOLR_CONNECTIONS={'default': settings.SOLR_CONNECTIONS['test']}):
            output = StringIO("")
            call_command('solr_schema', stdout=output)
            output.seek(0)
            output = output.read()
            assert 'Added ' in output
            assert 'Updated ' not in output

    @pytest.mark.usefixtures("solr")
    def test_update_solr(self):
        with override_settings(SOLR_CONNECTIONS={'default': settings.SOLR_CONNECTIONS['test']}):
            output = StringIO("")
            call_command('solr_schema', stdout=output)
            output.seek(0)
            output = output.read()
            assert 'Updated ' in output
            assert 'Added ' not in output

@patch('ppa.archive.solr.get_solr_connection')
class TestPagedSolrQuery(TestCase):

    def test_init(self, mock_get_solr_connection):
        mocksolr = Mock()
        coll = 'testcoll'
        mock_get_solr_connection.return_value = (mocksolr, coll)

        psq = PagedSolrQuery()
        assert psq.solr == mocksolr
        assert psq.solr_collection == coll
        assert psq.query_opts == {}

        opts = {'q': '*:*'}
        psq = PagedSolrQuery(query_opts=opts)
        assert psq.query_opts == opts

    def test_get_results(self, mock_get_solr_connection):
        mocksolr = Mock()
        coll = 'testcoll'
        mock_get_solr_connection.return_value = (mocksolr, coll)
        psq = PagedSolrQuery()
        results = psq.get_results()
        mocksolr.query.assert_called_once_with(coll, psq.query_opts)
        assert results == mocksolr.query.return_value.docs

    def test_count(self, mock_get_solr_connection):
        mocksolr = Mock()
        coll = 'testcoll'
        mock_get_solr_connection.return_value = (mocksolr, coll)
        mocksolr.query.return_value.get_num_found.return_value = 42
        psq = PagedSolrQuery()
        assert psq.count() == 42

    def test_get_json(self, mock_get_solr_connection):
        mocksolr = Mock()
        coll = 'testcoll'
        mock_get_solr_connection.return_value = (mocksolr, coll)
        mocksolr.query.return_value.get_num_found.return_value = 42
        psq = PagedSolrQuery()
        assert psq.get_json() == mocksolr.query.return_value.get_json.return_value

    def test_set_limits(self, mock_get_solr_connection):
        mock_get_solr_connection.return_value = (Mock(), 'coll')
        psq = PagedSolrQuery()
        psq.set_limits(1, 10)
        assert psq.query_opts['start'] == 1
        assert psq.query_opts['rows'] == 10
        psq.set_limits(100, 120)
        assert psq.query_opts['start'] == 100
        assert psq.query_opts['rows'] == 21

    def test_slice(self, mock_get_solr_connection):
        mocksolr = Mock()
        mock_get_solr_connection.return_value = (mocksolr, 'coll')

        psq = PagedSolrQuery()
        with patch.object(psq, 'set_limits') as mock_set_limits:
            # slice
            psq[:10]
            mock_set_limits.assert_any_call(None, 10)
            psq[4:10]
            mock_set_limits.assert_any_call(4, 10)
            psq[20:]
            mock_set_limits.assert_any_call(20, None)

            with patch.object(psq, 'get_results') as mock_get_results:
                mock_get_results.return_value = [3,]
                assert psq[0] == 3
                mock_set_limits.assert_any_call(0, 1)


class TestDigitizedWorkDetailView(TestCase):

    fixtures = ['sample_digitized_works']

    def test_get_context(self):

        # get a work and its detail page to test with
        dial = DigitizedWork.objects.get(source_id='chi.78013704')
        url = reverse('archive:detail', kwargs={'source_id': dial.source_id})

        # get the detail view page and check that the response is 200
        response = self.client.get(url)
        assert response.status_code == 200

        # now check that the right template is used
        assert 'archive/digitizedwork_detail.html' in \
            [template.name for template in response.templates]

        # check that the appropriate item is in context
        assert 'object' in response.context
        assert response.context['object'] == dial

    def test_template_rendering(self):
        # get a work and its detail page to test with
        wintry = DigitizedWork.objects.get(source_id='chi.13880510')
        url = reverse('archive:detail', kwargs={'source_id': wintry.source_id})

        # get the detail view page and check that the response is 200
        response = self.client.get(url)
        assert response.status_code == 200

        # - check that the information we expect is displayed
        # TODO: Make these HTML when the page is styled
        # hathitrust ID
        self.assertContains(
            response, wintry.title, count=2,
            msg_prefix='Missing two instance of object.title'
        )
        self.assertContains(
            response, wintry.source_id,
            msg_prefix='Missing HathiTrust ID (source_id)'
        )
        self.assertContains(
            response, wintry.source_url,
            msg_prefix='Missing source_url'
        )
        self.assertContains(
            response, wintry.enumcron,
            msg_prefix='Missing volume/chronology (enumcron)'
        )
        self.assertContains(
            response, wintry.author,
            msg_prefix='Missing author'
        )
        self.assertContains(
            response, wintry.pub_place,
            msg_prefix='Missing place of publication (pub_place)'
        )
        self.assertContains(
            response, wintry.publisher,
            msg_prefix='Missing publisher'
        )
        self.assertContains(
            response, wintry.pub_date,
            msg_prefix='Missing publication date (pub_date)'
        )
        self.assertContains(
            response, wintry.added.strftime("%d Dec %Y"),
            msg_prefix='Missing added or in wrong format (d M Y in filter)'
        )
        self.assertContains(
            response, wintry.updated.strftime("%d Dec %Y"),
            msg_prefix='Missing updated or in wrong format (d M Y in filter)'
        )
