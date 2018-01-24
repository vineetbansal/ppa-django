from django.conf.urls import url

from ppa.archive import views


urlpatterns = [
    url('^collections/$', views.CollectionListView.as_view(),
        name='list-collections'),
    url('^csv/$', views.DigitizedWorkCSV.as_view(), name='csv'),
    url('^(?P<source_id>.+)/$',
        views.DigitizedWorkDetailView.as_view(), name='detail'),
    url('^$', views.DigitizedWorkListView.as_view(), name='list'),
]
