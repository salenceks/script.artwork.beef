import random
import xbmc
import xbmcgui
from datetime import timedelta

from lib import cleaner, reporting
from lib.artworkselection import prompt_for_artwork
from lib.gatherer import Gatherer
from lib.providers import search
from lib.libs import mediainfo as info, mediatypes, pykodi, quickjson
from lib.libs.addonsettings import settings
from lib.libs.processeditems import ProcessedItems
from lib.libs.pykodi import datetime_now, localize as L, log
from lib.libs.utils import SortedDisplay, natural_sort, get_pathsep

MODE_AUTO = 'auto'
MODE_GUI = 'gui'

THROTTLE_TIME = 0.15

SOMETHING_MISSING = 32001
FINAL_MESSAGE = 32019
ADDING_ARTWORK_MESSAGE = 32020
NOT_AVAILABLE_MESSAGE = 32021
ARTWORK_UPDATED_MESSAGE = 32022
NO_ARTWORK_UPDATED_MESSAGE = 32023
PROVIDER_ERROR_MESSAGE = 32024
NOT_SUPPORTED_MESSAGE = 32025
CURRENT_ART = 13512
ENTER_COLLECTION_NAME = 32057

class ArtworkProcessor(object):
    def __init__(self, monitor=None):
        self.monitor = monitor or xbmc.Monitor()
        self.language = None
        self.autolanguages = None
        self.progress = xbmcgui.DialogProgressBG()
        self.visible = False
        self.freshstart = "0"
        self.processed = ProcessedItems()

    def create_progress(self):
        if not self.visible:
            self.progress.create("Artwork Beef: " + L(ADDING_ARTWORK_MESSAGE), "")
            self.visible = True

    def update_progress(self, percent, message):
        if self.visible:
            self.progress.update(percent, message=message)

    def close_progress(self):
        if self.visible:
            self.progress.close()
            self.visible = False

    def init_run(self, show_progress=False):
        self.setlanguages()
        self.freshstart = str(datetime_now() - timedelta(days=365))
        if show_progress:
            self.create_progress()

    def finish_run(self):
        self.close_progress()

    @property
    def processor_busy(self):
        # DEPRECATED: StringCompare is deprecated in Krypton, gone in Leia
        return pykodi.get_conditional('![StringCompare(Window(Home).Property(ArtworkBeef.Status),idle) | String.IsEqual(Window(Home).Property(ArtworkBeef.Status),idle)]')

    def process_item(self, mediatype, dbid, mode):
        if self.processor_busy:
            return
        if mode == MODE_GUI:
            busy = pykodi.get_busydialog()
            busy.create()
        if mediatype == mediatypes.TVSHOW:
            mediaitem = quickjson.get_tvshow_details(dbid)
        elif mediatype == mediatypes.MOVIE:
            mediaitem = quickjson.get_movie_details(dbid)
        elif mediatype == mediatypes.EPISODE:
            mediaitem = quickjson.get_episode_details(dbid)
        elif mediatype == mediatypes.MOVIESET:
            mediaitem = quickjson.get_movieset_details(dbid)
        else:
            if mode == MODE_GUI:
                busy.close()
            xbmcgui.Dialog().notification("Artwork Beef", L(NOT_SUPPORTED_MESSAGE).format(mediatype), '-', 6500)
            return

        self.init_run()
        if mediatype == mediatypes.EPISODE:
            series = quickjson.get_tvshow_details(mediaitem['tvshowid'])
            if series['imdbnumber'] not in settings.autoadd_episodes:
                mediaitem['skip'] = ['fanart']

        if mode == MODE_GUI:
            self.add_additional_iteminfo(mediaitem)
            self._process_item(Gatherer(self.monitor, settings.only_filesystem, self.autolanguages), mediaitem, True, False)
            busy.close()
            if 'available art' in mediaitem:
                availableart = mediaitem['available art']
                if 'seasons' in mediaitem and 'fanart' in availableart:
                    for season in mediaitem['seasons'].keys():
                        unseasoned_backdrops = [dict(art) for art in availableart['fanart'] if not art.get('hasseason')]
                        key = 'season.{0}.fanart'.format(season)
                        if key in availableart:
                            availableart[key].extend(unseasoned_backdrops)
                        else:
                            availableart[key] = unseasoned_backdrops
                tag_forcedandexisting_art(availableart, mediaitem['forced art'], mediaitem['art'])
                selectedarttype, selectedart = prompt_for_artwork(mediatype, mediaitem['label'],
                    availableart, self.monitor)
                if selectedarttype and selectedarttype not in availableart:
                    self.identify_movieset(mediaitem)
                    return
                if selectedarttype and selectedart:
                    if mediatypes.get_artinfo(mediatype, selectedarttype)['multiselect']:
                        existingurls = [url for exacttype, url in mediaitem['art'].iteritems()
                            if info.arttype_matches_base(exacttype, selectedarttype)]
                        urls_toset = [url for url in existingurls if url not in selectedart[1]]
                        urls_toset.extend([url for url in selectedart[0] if url not in urls_toset])
                        selectedart = dict(info.iter_renumbered_artlist(urls_toset, selectedarttype, mediaitem['art'].keys()))
                    else:
                        selectedart = {selectedarttype: selectedart}

                    selectedart = info.get_artwork_updates(mediaitem['art'], selectedart)
                    if selectedart:
                        mediaitem['selected art'] = selectedart
                        mediaitem['updated art'] = selectedart.keys()
                        add_art_to_library(mediatype, mediaitem.get('seasons'), mediaitem['dbid'], selectedart)
                    reporting.report_item(mediaitem, True, True)
                    notifycount(len(selectedart))
            else:
                xbmcgui.Dialog().notification(L(NOT_AVAILABLE_MESSAGE),
                    L(SOMETHING_MISSING) + ' ' + L(FINAL_MESSAGE), '-', 8000)
            self.finish_run()
        else:
            medialist = [mediaitem]
            if mediatype == mediatypes.TVSHOW:
                if mediaitem['imdbnumber'] in settings.autoadd_episodes:
                    medialist.extend(quickjson.get_episodes(dbid))
                elif settings.generate_episode_thumb:
                    for episode in quickjson.get_episodes(dbid):
                        if not info.has_generated_thumbnail(episode):
                            episode['skip'] = ['fanart']
                            medialist.append(episode)
            self.process_medialist(medialist, True)

    def process_medialist(self, medialist, alwaysnotify=False):
        self.init_run(len(medialist) > 0)
        artcount = 0
        currentitem = 0
        aborted = False
        for mediaitem in medialist:
            self.add_additional_iteminfo(mediaitem)
        singleitemlist = len(medialist) == 1
        if not singleitemlist:
            reporting.report_start(medialist)
        if medialist:
            gatherer = Gatherer(self.monitor, settings.only_filesystem, self.autolanguages)
        for mediaitem in medialist:
            self.update_progress(currentitem * 100 // len(medialist), mediaitem['label'])
            currentitem += 1
            if not info.is_known_mediatype(mediaitem):
                continue
            services_hit = self._process_item(gatherer, mediaitem)
            reporting.report_item(mediaitem, singleitemlist)
            if 'updated art' in mediaitem:
                artcount += len(mediaitem['updated art'])

            if not services_hit:
                if self.monitor.abortRequested():
                    aborted = True
                    break
            elif self.monitor.waitForAbort(THROTTLE_TIME):
                aborted = True
                break

        self.finish_run()
        reporting.report_end(medialist, currentitem if aborted else 0)
        if artcount or alwaysnotify:
            notifycount(artcount)
        return not aborted

    def _process_item(self, gatherer, mediaitem, singleitem=False, auto=True):
        mediatype = mediaitem['mediatype']
        if not mediaitem['imdbnumber'] and (singleitem or not settings.only_filesystem):
            if mediatype == mediatypes.MOVIESET:
                header = "Could not find set on TheMovieDB, can't process"
                message = "movie set '{0}'".format(mediaitem['label'])
                xbmcgui.Dialog().notification("Artwork Beef: " + header, message, xbmcgui.NOTIFICATION_INFO)
            else:
                header = "No default uniqueid available, can't process"
                message = "{0} '{1}'".format(mediatype, mediaitem['label'])
                if singleitem:
                    xbmcgui.Dialog().notification("Artwork Beef: " + header, message, xbmcgui.NOTIFICATION_INFO)

            if not singleitem or (mediatype == mediatypes.MOVIESET and not self.identify_movieset(mediaitem)):
                mediaitem['error'] = header
                if auto:
                    # Set nextdate to avoid repeated querying when no match is found
                    self.processed.set_nextdate(mediaitem['dbid'], mediatype, mediaitem['label'],
                        datetime_now() + timedelta(days=plus_some(15, 5)))
                return False

        if auto:
            cleaned = info.get_artwork_updates(mediaitem['art'], cleaner.clean_artwork(mediaitem))
            if cleaned:
                add_art_to_library(mediatype, mediaitem.get('seasons'), mediaitem['dbid'], cleaned)
                mediaitem['art'].update(cleaned)
                mediaitem['art'] = dict(item for item in mediaitem['art'].iteritems() if item[1])
                mediaitem['updated art'] = cleaned.keys()

        existingkeys = [key for key, url in mediaitem['art'].iteritems() if url]
        mediaitem['missing art'] = list(info.iter_missing_arttypes(mediaitem, existingkeys))

        forcedart, availableart, services_hit, error = gatherer.getartwork(mediaitem, auto)
        mediaitem['forced art'] = forcedart

        for arttype, imagelist in availableart.iteritems():
            self.sort_images(arttype, imagelist, mediaitem.get('file'))
        mediaitem['available art'] = availableart

        if auto:
            # Remove existing local artwork if it is no longer available
            existingart = dict(mediaitem['art'])
            localart = [(arttype, image['url']) for arttype, image in forcedart.iteritems()
                if not image['url'].startswith('http')]
            selectedart = dict((arttype, None) for arttype, url in existingart.iteritems()
                if not url.startswith(('http', 'image://video@')) and arttype not in ('animatedposter', 'animatedfanart')
                    and (arttype, url) not in localart)

            selectedart.update((key, image['url']) for key, image in forcedart.iteritems())
            selectedart = info.renumber_all_artwork(selectedart)

            existingart.update(selectedart)

            # Then add the rest of the missing art
            existingkeys = [key for key, url in existingart.iteritems() if url]
            selectedart.update(self.get_top_missing_art(info.iter_missing_arttypes(mediaitem, existingkeys),
                mediatype, existingart, availableart))

            # Identify actual changes, and save them
            selectedart = info.get_artwork_updates(mediaitem['art'], selectedart)
            if selectedart:
                mediaitem['selected art'] = selectedart
                mediaitem['updated art'] = list(set(mediaitem.get('updated art', []) + selectedart.keys()))
                add_art_to_library(mediatype, mediaitem.get('seasons'), mediaitem['dbid'], mediaitem['selected art'])

        if error:
            if 'message' in error:
                header = L(PROVIDER_ERROR_MESSAGE).format(error['providername'])
                msg = '{0}: {1}'.format(header, error['message'])
                mediaitem['error'] = msg
                log(msg)
                xbmcgui.Dialog().notification(header, error['message'], xbmcgui.NOTIFICATION_WARNING)
        elif auto:
            if not (mediatype == mediatypes.EPISODE and 'fanart' in mediaitem.get('skip', ())):
                self.processed.set_nextdate(mediaitem['dbid'], mediatype, mediaitem['label'],
                    datetime_now() + timedelta(days=self.get_nextcheckdelay(mediaitem)))
            if mediatype == mediatypes.TVSHOW:
                self.processed.set_data(mediaitem['dbid'], mediatype, mediaitem['label'], mediaitem['season'])
        return services_hit

    def get_nextcheckdelay(self, mediaitem):
        if settings.only_filesystem:
            return plus_some(5, 3)
        elif not mediaitem.get('missing art'):
            return plus_some(120, 25)
        elif mediaitem['mediatype'] in (mediatypes.MOVIE, mediatypes.TVSHOW) and \
                mediaitem['premiered'] > self.freshstart:
            return plus_some(30, 10)
        else:
            return plus_some(60, 15)

    def identify_movieset(self, mediaitem):
        self.add_additional_iteminfo(mediaitem)
        uniqueid = None
        while not uniqueid:
            result = xbmcgui.Dialog().input(L(ENTER_COLLECTION_NAME), mediaitem['label'])
            if not result:
                return False # Cancelled
            options = search.search(result, mediatypes.MOVIESET)
            selected = xbmcgui.Dialog().select(mediaitem['label'], [option['label'] for option in options])
            if selected < 0:
                return False # Cancelled
            uniqueid = options[selected]['id']
        mediaitem['imdbnumber'] = uniqueid
        self.processed.set_data(mediaitem['setid'], mediatypes.MOVIESET, mediaitem['label'], uniqueid)
        return True

    def setlanguages(self):
        self.language = pykodi.get_language(xbmc.ISO_639_1)
        self.autolanguages = (self.language, None) if self.language == 'en' else (self.language, 'en', None)
        if settings.language_override:
            self.language = settings.language_override
        if settings.language_override not in self.autolanguages:
            self.autolanguages += (settings.language_override,)

    def add_additional_iteminfo(self, mediaitem):
        if 'mediatype' in mediaitem:
            return
        info.prepare_mediaitem(mediaitem)
        if mediaitem['mediatype'] == mediatypes.EPISODE:
            mediaitem['imdbnumber'] = self._get_episodeid(mediaitem)
        elif mediaitem['mediatype'] == mediatypes.TVSHOW:
            mediaitem['seasons'], seasonart = self._get_seasons_artwork(quickjson.get_seasons(mediaitem['dbid']))
            mediaitem['art'].update(seasonart)
        elif mediaitem['mediatype'] == mediatypes.MOVIESET:
            uniqueid = self.processed.get_data(mediaitem['dbid'], mediaitem['mediatype'])
            if not uniqueid and not settings.only_filesystem:
                uniqueid = search.for_id(mediaitem['label'], mediatypes.MOVIESET)
                if uniqueid:
                    self.processed.set_data(mediaitem['dbid'], mediatypes.MOVIESET, mediaitem['label'], uniqueid)
                else:
                    log("Could not find set '{0}' on TheMovieDB".format(mediaitem['label']), xbmc.LOGNOTICE)

            mediaitem['imdbnumber'] = uniqueid
            new = not self.processed.exists(mediaitem['dbid'], mediaitem['mediatype'], mediaitem['label'])
            if settings.setartwork_fromparent or new:
                prepare_movieset(mediaitem, new, settings.setartwork_fromparent)

            if 'file' not in mediaitem and settings.setartwork_fromcentral and settings.setartwork_dir:
                mediaitem['file'] = settings.setartwork_dir + mediaitem['label'] + '.ext'

    def sort_images(self, arttype, imagelist, mediapath):
        # 1. Language, preferring fanart with no language/title if configured
        # 2. Match discart to media source
        # 3. Size (in 200px groups), up to preferredsize
        # 4. Rating
        imagelist.sort(key=lambda image: image['rating'].sort, reverse=True)
        imagelist.sort(key=self.size_sort, reverse=True)
        if arttype == 'discart':
            mediasubtype = info.get_media_source(mediapath)
            if mediasubtype != 'unknown':
                imagelist.sort(key=lambda image: 0 if image.get('subtype', SortedDisplay(None, '')).sort == mediasubtype else 1)
        imagelist.sort(key=lambda image: self._imagelanguage_sort(image, arttype))

    def size_sort(self, image):
        imagesplit = image['size'].display.split('x')
        if len(imagesplit) != 2:
            return image['size'].sort
        try:
            imagesize = int(imagesplit[0]), int(imagesplit[1])
        except ValueError:
            return image['size'].sort
        if imagesize[0] > settings.preferredsize[0]:
            shrink = settings.preferredsize[0] / float(imagesize[0])
            imagesize = settings.preferredsize[0], imagesize[1] * shrink
        if imagesize[1] > settings.preferredsize[1]:
            shrink = settings.preferredsize[1] / float(imagesize[1])
            imagesize = imagesize[0] * shrink, settings.preferredsize[1]
        return max(imagesize) // 200

    def _imagelanguage_sort(self, image, arttype):
        primarysort = 0 if image['language'] == self.language else \
            0.5 if self.language != 'en' and image['language'] == 'en' else 1

        if image['language'] and (arttype.endswith('fanart') and settings.titlefree_fanart or
                arttype.endswith('poster') and settings.titlefree_poster):
            primarysort += 1

        return primarysort, image['language']

    def get_top_missing_art(self, missingarts, mediatype, existingart, availableart):
        if not availableart:
            return {}
        newartwork = {}
        for missingart in missingarts:
            if missingart not in availableart:
                continue
            itemtype, artkey = mediatypes.hack_mediaarttype(mediatype, missingart)
            artinfo = mediatypes.get_artinfo(itemtype, artkey)
            if artinfo['multiselect']:
                existingurls = []
                existingartnames = []
                for art, url in existingart.iteritems():
                    if info.arttype_matches_base(art, missingart) and url:
                        existingurls.append(url)
                        existingartnames.append(art)

                newart = [art for art in availableart[missingart] if self._auto_filter(missingart, art, existingurls)]
                if not newart:
                    continue
                newartcount = 0
                for i in range(0, artinfo['autolimit']):
                    exacttype = '%s%s' % (artkey, i if i else '')
                    if exacttype not in existingartnames:
                        if newartcount >= len(newart):
                            break
                        if exacttype not in newartwork:
                            newartwork[exacttype] = []
                        newartwork[exacttype] = newart[newartcount]['url']
                        newartcount += 1
            else:
                newart = next((art for art in availableart[missingart] if self._auto_filter(missingart, art)), None)
                if newart:
                    newartwork[missingart] = newart['url']
        return newartwork

    def _get_seasons_artwork(self, seasons):
        resultseasons = {}
        resultart = {}
        for season in seasons:
            resultseasons[season['season']] = season['seasonid']
            for arttype, url in season['art'].iteritems():
                arttype = arttype.lower()
                if not arttype.startswith(('tvshow.', 'season.')):
                    resultart['%s.%s.%s' % (mediatypes.SEASON, season['season'], arttype)] = pykodi.unquoteimage(url)
        return resultseasons, resultart

    def _get_episodeid(self, episode):
        if 'unknown' in episode['uniqueid']:
            return episode['uniqueid']['unknown']
        else:
            idsource, result = episode['uniqueid'].iteritems()[0] if episode['uniqueid'] else '', ''
            if result:
                log("Didn't find 'unknown' uniqueid for episode, just picked the first, from '%s'." % idsource, xbmc.LOGINFO)
            else:
                log("Didn't find a uniqueid for episode '%s', can't look it up. I expect the ID from TheTVDB, which generally comes from the scraper." % episode['label'], xbmc.LOGNOTICE)
            return result

    def _auto_filter(self, arttype, art, ignoreurls=()):
        if art['rating'].sort < settings.minimum_rating:
            return False
        if arttype.endswith('fanart') and art['size'].sort < settings.minimum_size:
            return False
        return art['language'] in self.autolanguages and art['url'] not in ignoreurls

def add_art_to_library(mediatype, seasons, dbid, selectedart):
    if not selectedart:
        return
    if mediatype == mediatypes.TVSHOW:
        for season, season_id in seasons.iteritems():
            info.update_art_in_library(mediatypes.SEASON, season_id, dict((arttype.split('.')[2], url)
                for arttype, url in selectedart.iteritems() if arttype.startswith('season.{0}.'.format(season))))
        info.update_art_in_library(mediatype, dbid, dict((arttype, url)
            for arttype, url in selectedart.iteritems() if '.' not in arttype))
    else:
        info.update_art_in_library(mediatype, dbid, selectedart)

def notifycount(count):
    if count:
        xbmcgui.Dialog().notification("Artwork Beef: " + L(ARTWORK_UPDATED_MESSAGE).format(count), L(FINAL_MESSAGE), '-', 7500)
    else:
        xbmcgui.Dialog().notification("Artwork Beef: " + L(NO_ARTWORK_UPDATED_MESSAGE),
            L(SOMETHING_MISSING) + ' ' + L(FINAL_MESSAGE), '-', 8000)

def plus_some(start, rng):
    return start + (random.randrange(-rng, rng + 1))

def prepare_movieset(mediaitem, new, setfile):
    if new:
        # Remove poster/fanart Kodi automatically sets from a movie
        if not set(key for key in mediaitem['art']).difference('poster', 'fanart'):
            if 'poster' in mediaitem['art']:
                del mediaitem['art']['poster']
            if 'fanart' in mediaitem['art']:
                del mediaitem['art']['fanart']

    if setfile:
        # Identify set folder among movie parent dirs
        if 'movies' not in mediaitem:
            mediaitem['movies'] = quickjson.get_movieset_details(mediaitem['dbid'])['movies']
        for movie in mediaitem['movies']:
            pathsep = get_pathsep(movie['file'])
            setmatch = pathsep + mediaitem['label'] + pathsep
            if setmatch in movie['file']:
                mediaitem['file'] = movie['file'].split(setmatch)[0] + setmatch

            if 'file' in mediaitem:
                break

def tag_forcedandexisting_art(availableart, forcedart, existingart):
    typeinsert = {}
    for exacttype, artlist in sorted(forcedart.iteritems(), key=lambda arttype: natural_sort(arttype[0])):
        arttype = info.get_basetype(exacttype)
        if arttype not in availableart:
            availableart[arttype] = artlist
        else:
            for image in artlist:
                match = next((available for available in availableart[arttype] if available['url'] == image['url']), None)
                if match:
                    if 'title' in image and 'title' not in match:
                        match['title'] = image['title']
                    match['second provider'] = image['provider'].display
                else:
                    typeinsert[arttype] = typeinsert[arttype] + 1 if arttype in typeinsert else 0
                    availableart[arttype].insert(typeinsert[arttype], image)

    typeinsert = {}
    for exacttype, existingurl in existingart.iteritems():
        arttype = info.get_basetype(exacttype)
        if arttype in availableart:
            match = next((available for available in availableart[arttype] if available['url'] == existingurl), None)
            if match:
                match['preview'] = existingurl
                match['existing'] = True
            else:
                typeinsert[arttype] = typeinsert[arttype] + 1 if arttype in typeinsert else 0
                image = {'url': existingurl, 'preview': existingurl, 'title': exacttype,
                    'existing': True, 'provider': SortedDisplay('current', L(CURRENT_ART))}
                availableart[arttype].insert(typeinsert[arttype], image)