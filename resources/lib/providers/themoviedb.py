import requests
import xbmc

from devhelper.pykodi import log

import mediatypes
import providers
from base import AbstractProvider
from sorteddisplaytuple import SortedDisplay

# TODO: I can get good sized thumbnails from tmdb with 'w780' instead of 'original' in the URL
# TODO: Reweigh ratings, as this does the exact opposite of thetvdb, and weighs each single rating very lowly, images rarely hit 6 stars, nor go lower than 4.7
class TheMovieDBProvider(AbstractProvider):
    name = 'themoviedb.org'
    mediatype = mediatypes.MOVIE

    apikey = '***REMOVED***'
    cfgurl = 'http://api.themoviedb.org/3/configuration'
    apiurl = 'http://api.themoviedb.org/3/movie/%s/images'
    artmap = {'backdrops': 'fanart', 'posters': 'poster'}

    def __init__(self):
        super(TheMovieDBProvider, self).__init__()
        self.session.headers['Accept'] = 'application/json'

    def log(self, message, level=xbmc.LOGDEBUG):
        log(message, level, tag=self.name)

    def _get_base_url(self):
        response = self.session.get(self.cfgurl, params={'api_key': self.apikey}, timeout=5)
        return response.json()['images']['base_url']

    def get_images(self, mediaid):
        self.log("Getting art for '%s'." % mediaid)
        response = self.session.get(self.apiurl % mediaid, params={'api_key': self.apikey}, timeout=5)
        if response.status_code == requests.codes.not_found:
            return {}
        response.raise_for_status()
        base_url = self._get_base_url()
        data = response.json()
        result = {}
        for arttype, artlist in data.iteritems():
            generaltype = self.artmap.get(arttype)
            if not generaltype:
                continue
            if artlist and generaltype not in result:
                result[generaltype] = []
            for image in artlist:
                resultimage = {'url': base_url + 'original' + image['file_path'], 'provider': self.name}
                resultimage['language'] = image['iso_639_1']
                if image['vote_count']:
                    resultimage['rating'] = SortedDisplay(image['vote_average'], '{0:.1f}'.format(image['vote_average']))
                else:
                    resultimage['rating'] = SortedDisplay(5, 'Not rated')
                resultimage['size'] = SortedDisplay(image['width'], '%sx%s' % (image['width'], image['height']))
                if arttype == 'poster' and image['aspect_ratio'] > 0.685 or image['aspect_ratio'] < 0.66:
                    resultimage['status'] = providers.GOOFY_IMAGE
                result[generaltype].append(resultimage)
        return result