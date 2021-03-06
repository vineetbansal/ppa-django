from collections import OrderedDict
from unittest.mock import patch, Mock
from json.decoder import JSONDecodeError

from django.conf import settings
from django.contrib.admin.models import LogEntry, ADDITION
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
import pytest

from ppa.archive import hathi
from ppa.archive.models import DigitizedWork
from ppa.archive.util import HathiImporter


class TestHathiImporter(TestCase):
    fixtures = ['sample_digitized_works']

    def test_filter_existing_ids(self):

        digwork_ids = DigitizedWork.objects.values_list('source_id', flat=True)

        # all existing - all should be flagged as existing
        htimporter = HathiImporter(digwork_ids)
        htimporter.filter_existing_ids()
        # no ht ids left, all marked existing
        assert not htimporter.htids
        assert len(htimporter.existing_ids) == len(digwork_ids)
        # existing_ids should be dict with source id -> pk
        digwork = DigitizedWork.objects.first()
        assert htimporter.existing_ids[digwork.source_id] == digwork.pk

        # mix of new and existing ids
        new_ids = ['one', 'two', 'three']
        htimporter = HathiImporter(new_ids + list(digwork_ids))
        htimporter.filter_existing_ids()
        assert len(htimporter.existing_ids) == len(digwork_ids)
        assert set(htimporter.htids) == set(new_ids)

    @patch('ppa.archive.models.DigitizedWork.add_from_hathi')
    def test_add_items(self, mock_add_from_hathi):
        test_htid = 'a:123'
        htimporter = HathiImporter([test_htid])
        # simulate record not found
        mock_add_from_hathi.side_effect = hathi.HathiItemNotFound
        htimporter.add_items()
        mock_add_from_hathi.assert_called_with(
            test_htid, htimporter.bib_api, get_data=True,
            log_msg_src=None, user=None)
        assert not htimporter.imported_works
        # actual error stored in results
        assert isinstance(htimporter.results[test_htid], hathi.HathiItemNotFound)
        # no partial record hanging around
        assert not DigitizedWork.objects.filter(source_id=test_htid)

        # simulate permission denied
        mock_add_from_hathi.side_effect = hathi.HathiItemForbidden
        log_msg_src = 'from unit test'
        htimporter.add_items(log_msg_src)
        mock_add_from_hathi.assert_called_with(
            test_htid, htimporter.bib_api, get_data=True,
            log_msg_src=log_msg_src, user=None)
        # actual error stored in results
        assert isinstance(htimporter.results[test_htid], hathi.HathiItemForbidden)
        # no partial record hanging aruond
        assert not DigitizedWork.objects.filter(source_id=test_htid)

        # simulate success
        def fake_add_from_hathi(htid, *args, **kwargs):
            '''fake add_from_hathi method to create
            a new digwork and corresponding log entry'''
            digwork = DigitizedWork.objects.create(source_id=htid,
                                                   page_count=1337)
            # create log entry for record creation
            LogEntry.objects.log_action(
                user_id=User.objects.get(username=settings.SCRIPT_USERNAME).pk,
                content_type_id=ContentType.objects.get_for_model(digwork).pk,
                object_id=digwork.pk,
                object_repr=str(digwork),
                change_message='Created %s' % log_msg_src,
                action_flag=ADDITION)

            return digwork

        mock_add_from_hathi.side_effect = fake_add_from_hathi
        htimporter.add_items(log_msg_src)
        assert len(htimporter.imported_works) == 1
        assert htimporter.results[test_htid] == HathiImporter.SUCCESS

    @patch('ppa.archive.util.DigitizedWork')
    @patch('ppa.archive.util.get_solr_connection')
    def test_index(self, mock_get_solr, mock_digitizedwork):
        test_htid = 'a:123'
        htimporter = HathiImporter([test_htid])
        # no imported works, index should do nothing
        htimporter.index()
        mock_digitizedwork.index_items.assert_not_called()
        mock_get_solr.assert_not_called()

        # simulate imported work to index
        mocksolr = Mock()
        mockcollection = Mock()
        mock_get_solr.return_value = mocksolr, mockcollection
        mock_digwork = Mock()
        htimporter.imported_works = [mock_digwork]
        htimporter.index()
        mock_digitizedwork.index_items.assert_any_call(htimporter.imported_works)
        mock_digitizedwork.index_items.assert_any_call(mock_digwork.page_index_data())

        mock_get_solr.assert_called_with()
        mocksolr.commit.assert_called_with(mockcollection, openSearcher=True)

    def test_get_status_message(self):
        htimporter = HathiImporter(['a:123'])
        # simple status codes
        assert htimporter.get_status_message(HathiImporter.SUCCESS) == \
            HathiImporter.status_message[HathiImporter.SUCCESS]
        assert htimporter.get_status_message(HathiImporter.SKIPPED) == \
            HathiImporter.status_message[HathiImporter.SKIPPED]
        # error classses
        assert htimporter.get_status_message(hathi.HathiItemNotFound()) == \
            HathiImporter.status_message[hathi.HathiItemNotFound]
        assert htimporter.get_status_message(hathi.HathiItemForbidden()) == \
            HathiImporter.status_message[hathi.HathiItemForbidden]
        assert htimporter.get_status_message(Mock(spec=JSONDecodeError)) == \
            HathiImporter.status_message[JSONDecodeError]

        # error for anything else
        with pytest.raises(KeyError):
            htimporter.get_status_message('foo')

    def test_output_results(self):
        htimporter = HathiImporter(['a:123'])
        # set sample results to test - one of each
        success_id = 'added:1'
        notfound_id = 'err:404'

        # htimporter.results = {
        htimporter.results = OrderedDict([
            (success_id, HathiImporter.SUCCESS),
            (notfound_id, hathi.HathiItemNotFound()),
        ])
        output_results = htimporter.output_results()
        # length of output results should match results
        assert len(output_results) == len(htimporter.results)
        # message should be set for each based on value or type of status
        assert output_results[success_id] == \
            HathiImporter.status_message[HathiImporter.SUCCESS]
        assert output_results[notfound_id] == \
            HathiImporter.status_message[hathi.HathiItemNotFound]
