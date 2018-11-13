from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from ppa.archive.models import DigitizedWork


class ArchiveViewsSitemap(Sitemap):
    '''Sitemap for archive views that are not Wagtail pages but also
    not tied to models.'''

    def items(self):
        # return list of view names
        return ['list', 'list-collections']

    def location(self, obj):
        # generate url based on archives url names
        return reverse('archive:{}'.format(obj))

    def lastmod(self, obj):
        # both pages are modified based on changes to digitized works,
        # so return the most recent modification time of any of them
        return DigitizedWork.objects.order_by('-updated').first().updated


class DigitizedWorkSitemap(Sitemap):
    '''Sitemap for :class:`~ppa.archive.models.DigitizedWork` detail
    pages.'''

    def items(self):
        return DigitizedWork.objects.all()

    def lastmod(self, obj):
        return obj.updated