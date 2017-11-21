# utilities for working with hathitrust materials and apis

import requests


class HathiBibliographicAPI(object):
    # https://www.hathitrust.org/bib_api

    api_root = 'http://catalog.hathitrust.org/api'

    def _get_record(self, mode, id_type, id_value):
        url = '%(base)s/volumes/%(mode)s/%(id_type)s/%(id_value)s.json' % {
             'base': self.api_root,
            'mode': mode,
            'id_type': id_type,
            'id_value': id_value # NOTE: / in ark ids is *not* escaped
        }
        resp = requests.get(url)
        # TODO: handle errors
        if resp.status_code == requests.codes.ok:
            return HathiBibliographicRecord(resp.json())


    def brief_record(self, id_type, id_value):
        return self._get_record('brief', id_type, id_value)

    def record(self, id_type, id_value):
        return self._get_record('full', id_type, id_value)

    # also possible: get multiple records at once


class HathiBibliographicRecord(object):

    def __init__(self, data):
        self._data = data
        # for a single bib api json result, we only want the first item
        self.record_id = list(data['records'].keys())[0]
        self.info = list(data['records'].values())[0]

    @property
    def title(self):
        # returns list of titles - standard title; could also have
        # title without leading article and other language titles
        return self.info['titles'][0]

    @property
    def pub_dates(self):
        return self.info['publishDates']

    def copy_details(self, htid):
        for item in self._data['items']:
            if item['htid'] == htid:
                return item



