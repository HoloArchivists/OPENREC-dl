#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta
from enum import Enum
import gevent
from gevent import monkey
monkey.patch_all()
from gevent.pool import Pool
import glob
from http import cookiejar
import json
import os
from progress.bar import IncrementalBar
import re
import requests
from requests_toolbelt import sessions
from subprocess import Popen, PIPE, STDOUT
import sys
from time import sleep
import urllib.parse

VERSION_STRING="2021.07.08.1"
ISSUES_URL="https://github.com/HoloArchivists/OPENREC-dl/issues"

OPENREC=r'^(?:https?:\/\/)?(?:www\.)?openrec\.tv\/(?P<type>[^\/]+?)\/(?P<id>[^\/]+)$'
VALID_LIVE_ID=r'^(?P<id>[a-zA-Z0-9]+?)$'
FULL_SIZE_IMG=r'\.w[0-9]{1,}\.ttl[0-9]{1,}\.(?P<ext>[a-z]{1,4})\?'
FULL_SIZE_REP=r'.\g<ext>?q=100&quality=100&'
CLEAN_FILENAME_KINDA=r'[^\w\-_\. \[\]\(\)]'
OLD_PL_HOST=r'^https?:\/\/openrec-live\.s3\.amazonaws\.com\/studio\/[0-9]{1,}\/(?P<vid>[0-9]{1,})\/index\.m3u8$'
NEW_PL_HOST=r'^https?:\/\/[a-z0-9]{1,}\.cloudfront\.net\/[a-f0-9]{1,}\/(?P<pname>[^\/]+)\.m3u8$'
GAME_PL_HOST=r'^https?:\/\/[a-z0-9]{1,}\.cloudfront\.net\/[0-9]{1,}\/[0-9]{1,}_[a-zA-Z]{1,}\/game\/(?P<pname>[^\/]+)\.m3u8$'
COOKIE_DOMAIN="www.openrec.tv"
PUBLIC_API="https://public.openrec.tv/external/api/v5/"
PRIVATE_API="https://apiv5.openrec.tv/api/v5/"
MAX_MOVIE_RESPONSE=40

NORMAL_MAP = {
    "url": "normal",
    "_url_playlist": "playlist",
    "url_public": "public",
    "url_audio": "aac",
    "url_source": "chunklist_source/chunklist",
    "url_high": "chunklist_high/chunklist",
    "url_medium": "chunklist_medium/chunklist",
    "url_low_latency": "chunklist_low/chunklist"
}

PLAYLIST_MAP = {
    "url": "playlist",
    "_url_normal": "normal",
    "url_public": "public",
    "url_audio": "aac",
    "url_source": "chunklist_source/chunklist",
    "url_high": "chunklist_high/chunklist",
    "url_medium": "chunklist_medium/chunklist",
    "url_low_latency": "chunklist_low/chunklist",
    "url_ull": "chunklist_144p/chunklist"
}

GAME_MAP = {
    "url_source": "source",
    "url_high": "2000kbps",
    "url_medium": "1000kbps"
}

class LogLevel(Enum):
    BASIC=1
    VERBOSE=2

class DownloadBar(IncrementalBar):
    suffix="%(percent).1f%%"
    @property
    def time_remaining(self):
        time_per_event = float(self.elapsed) / float(self.index)
        seconds_remaining = time_per_event * self.remaining
        m = seconds_remaining % 60
        s = seconds_remaining / 60
        return f"{int(m)}:{int(s):02}"

class StreamDownloader():
    def __init__(self, playlist_base):
        self.m3u8_session = sessions.BaseUrlSession(base_url=playlist_base)
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=10)
        self.m3u8_session.mount('http://', adapter)
        self.m3u8_session.mount('https://', adapter)
        self.success = True
        self.failed_list = []
        self.completed = {}
    
    def run(self, stream_filename, ts_list, download_bar):
        self.stream_filename = stream_filename
        self.stream_file = open(os.path.join(args.directory, f"{stream_filename}.ts.tmp"), "ab")
        self.ts_count = len(ts_list)
        self.download_bar = download_bar
        join_thread = gevent.spawn(self._append_file)
        self._download_segments(ts_list)
        join_thread.join()
    
    def _download_segments(self, ts_list):
        Pool(10).map(self._download_worker, ts_list)
        if not self.success:
            ts_list = self.failed_list
            self.failed_list = []
            self._download_segments(ts_list)
    
    def _download_worker(self, ts_tuple):
        ts_segment = ts_tuple[0]
        ts_index = ts_tuple[1]
        for retry in range(0, 5):
            try:
                ts_r = self.m3u8_session.get(ts_segment)
                if ts_r.ok:
                    segment_filename = f"{self.stream_filename}.seg{ts_index}"
                    with open(os.path.join(args.directory, segment_filename), 'wb') as segment_file:
                        segment_file.write(ts_r.content)
                    self.completed[ts_index] = segment_filename
                    return
            except:
                print_log("download worker", f"failed to download {ts_segment}, retrying ({retry}/5)...")
        self.success = False
    
    def _append_file(self):
        index = 0
        while index < self.ts_count:
            segment_filename = self.completed.get(index, '')
            if segment_filename:
                with open(os.path.join(args.directory, segment_filename), 'rb') as segment_file:
                    self.stream_file.write(segment_file.read())
                os.remove(os.path.join(args.directory, segment_filename))
                self.download_bar.next()
                index += 1
            else:
                sleep(0.05)
        self.stream_file.close()

def dl_channel(s, ps, channel_id):
    # check for argument that cannot be used
    if args.list_formats:
        print_log(f"channel:{channel_id}", "cannot use argument --list-formats with channel link")
        return
    
    # get base channel data for checking validity
    init_response = s.get(f'channels/{channel_id}')
    if not init_response.ok:
        print_log(f"channel:{channel_id}", "failed to get channel information")
        print_log(f"channel:{channel_id}", f"API response returned status code {init_response.status_code}", LogLevel.VERBOSE)
        return
    c_init_json = init_response.json()
    if "status" in c_init_json:
        print_log(f"channel:{channel_id}", "failed to get channel information")
        print_log(f"channel:{channel_id}", f"API body returned status code {c_init_json['status']}: {c_init_json['message']}", LogLevel.VERBOSE)
        return
    
    # string to use in output channel filenames
    channel_string = re.sub(CLEAN_FILENAME_KINDA, "_", f"{c_init_json['name']} [{c_init_json['id']}]").strip()

    # use search api to retrieve more complete json data
    search_query_param = urllib.parse.urlencode({"search_query": c_init_json["name"]})
    search_response = s.get(f'search-users?{search_query_param}')

    # use /channels json if /search-users json doesn't exist/can't be found for some reason
    c_json = c_init_json
    if search_response.ok:
        found_full_json = False
        for user_result in search_response.json():
            if user_result["name"] == c_init_json["name"] and user_result["openrec_user_id"] == c_init_json["openrec_user_id"]:
                c_json = user_result
                found_full_json = True
                break
        if not found_full_json:
            print_log(f"channel:{channel_id}", f"failed to get complete channel information from API")
    else:
        print_log(f"channel:{channel_id}", f"failed to get complete channel information from API")
        print_log(f"channel:{channel_id}", f"API response returned status code {search_response.status_code}", LogLevel.VERBOSE)

    if args.write_info_json:
        info_filename = f"{channel_string}.info.json"
        info_filepath = os.path.join(args.directory, info_filename)
        if os.path.isfile(f"{info_filepath}.tmp"):
            os.remove(f"{info_filepath}.tmp")
        print_log(f"info:{channel_id}", f"writing channel information to '{info_filename}'")
        with open(f"{info_filepath}.tmp", "w") as channel_info:
            channel_info.write(json.dumps(c_json))
        if os.path.isfile(info_filepath):
            os.remove(info_filepath)
        os.rename(f"{info_filepath}.tmp", info_filepath)
    
    if args.write_thumbnail:
        # icon (avatar)
        full_size_icon_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, c_json["icon_image_url"])
        icon_format = urllib.parse.parse_qs(full_size_icon_url)["format"][0]
        icon_filename = f"{channel_string}-icon.{icon_format}"
        icon_filepath = os.path.join(args.directory, icon_filename)
        if os.path.isfile(f"{icon_filepath}.tmp"):
            os.remove(f"{icon_filepath}.tmp")
        print_log(f"icon:{channel_id}", f"writing channel icon to '{icon_filename}'")
        icon_response = requests.get(full_size_icon_url)
        if icon_response.ok:
            with open(f"{icon_filepath}.tmp", "wb") as channel_icon:
                channel_icon.write(icon_response.content)
            if os.path.isfile(icon_filepath):
                os.remove(icon_filepath)
            os.rename(f"{icon_filepath}.tmp", icon_filepath)
        else:
            print_log(f"icon:{channel_id}", "failed to retrieve channel icon")
            print_log(f"icon:{channel_id}", f"API response returned status code {icon_response.status_code}", LogLevel.VERBOSE)
        
        # cover (banner)
        full_size_cover_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, c_json["cover_image_url"])
        cover_format = urllib.parse.parse_qs(full_size_cover_url)["format"][0]
        cover_filename = f"{channel_string}-cover.{cover_format}"
        cover_filepath = os.path.join(args.directory, cover_filename)
        if os.path.isfile(f"{cover_filepath}.tmp"):
            os.remove(f"{cover_filepath}.tmp")
        print_log(f"cover:{channel_id}", f"writing channel cover to '{cover_filename}'")
        cover_response = requests.get(full_size_icon_url)
        if cover_response.ok:
            with open(f"{cover_filepath}.tmp", "wb") as channel_cover:
                channel_cover.write(cover_response.content)
            if os.path.isfile(cover_filepath):
                os.remove(cover_filepath)
            os.rename(f"{cover_filepath}.tmp", cover_filepath)
        else:
            print_log(f"icon:{channel_id}", "failed to retrieve channel cover")
            print_log(f"icon:{channel_id}", f"API response returned status code {cover_response.status_code}", LogLevel.VERBOSE)

    # retrieve list of all channel movies from API
    movie_list = []
    movie_list_response = []
    movie_page_index = 1
    while len(movie_list_response) == MAX_MOVIE_RESPONSE or movie_page_index == 1:
        print_log(f"channel:{channel_id}", f"downloading videos page {movie_page_index}")
        movie_list_response = s.get(f"movies?channel_ids={channel_id}&page={movie_page_index}").json()
        movie_list.append(movie_list_response)
        movie_page_index += 1
    movie_list.append(movie_list_response)

    for movie_index in range(len(movie_list_response)):
        print_log(f"channel:{channel_id}", f"downloading video {movie_index + 1} of {len(movie_list_response)}")
        dl_movie(s, ps, movie_list_response[movie_index]["id"])

def dl_movie(s, ps, movie_id):
    # check if we can skip
    if args.download_archive:
        if os.path.isfile(args.download_archive):
            with open(args.download_archive, "r") as archive_file:
                skip_ids = archive_file.read().splitlines()
            if movie_id in skip_ids:
                print_log(f"movie:{movie_id}", "already recorded in archive")
                return

    # get public video data and check validity
    movie_response = s.get(f"movies/{movie_id}")
    if not movie_response.ok:
        print_log(f"info:{movie_id}", "failed to get movie information")
        print_log(f"info:{movie_id}", f"API response returned status code {movie_response.status_code}", LogLevel.VERBOSE)
        return
    m_json = movie_response.json()
    if "status" in m_json:
        print_log(f"info:{movie_id}", "failed to get movie information")
        print_log(f"info:{movie_id}", f"API body returned status code {m_json['status']}: {m_json['message']}", LogLevel.VERBOSE)
        return
    
    # string to use in output video names
    movie_string = re.sub(CLEAN_FILENAME_KINDA, "_", f"{m_json['title']} [{m_json['id']}]").strip()

    # remove ad info because we don't care about ads (atm)
    m_json.pop("ad")

    m_json["media"] = derive_media_playlists(movie_id, m_json["media"], ps)
    if not m_json["media"]:
        return
    elif "_url_playlist" in m_json["media"]:
        formats_list = get_m3u8_info(m_json["media"]["_url_playlist"])
    else:
        formats_list = get_m3u8_info(m_json["media"]["url"])

    if args.list_formats:
        print_log(f"movie:{movie_id}", f"available formats:")
        print_formats(formats_list)
        return
    
    downloading_format = None
    best_bitrate = 0
    for format_settings in formats_list:
        if args.format == "best":
            if format_settings["media"]["NAME"] == "Source":
                downloading_format = urllib.parse.urljoin(m_json["media"]["url"], format_settings["location"])
                break
            elif int(format_settings['format']['BANDWIDTH']) > best_bitrate:
                downloading_format = urllib.parse.urljoin(m_json["media"]["url"], format_settings["location"])
                best_bitrate = int(format_settings['format']['BANDWIDTH'])
        elif args.format in {format_settings["media"]["NAME"], format_settings["media"]["GROUP-ID"]}:
            downloading_format = urllib.parse.urljoin(m_json["media"]["url"], format_settings["location"])
            break

    if args.write_info_json:
        info_filename = f"{movie_string}.info.json"
        info_filepath = os.path.join(args.directory, info_filename)
        if os.path.isfile(f"{info_filepath}.tmp"):
            os.remove(f"{info_filepath}.tmp")
        print_log(f"info:{movie_id}", f"writing video information to '{info_filename}'")
        with open(f"{info_filepath}.tmp", "w") as movie_info:
            movie_info.write(json.dumps(m_json))
        if os.path.isfile(info_filepath):
            os.remove(info_filepath)
        os.rename(f"{info_filepath}.tmp", info_filepath)
    
    if args.write_thumbnail:
        full_size_thumb_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, m_json["thumbnail_url"])
        thumbnail_format = urllib.parse.parse_qs(full_size_thumb_url)["format"][0]
        thumbnail_filename = f"{movie_string}.{thumbnail_format}"
        thumbnail_filepath = os.path.join(args.directory, thumbnail_filename)
        if os.path.isfile(f"{thumbnail_filepath}.tmp"):
            os.remove(f"{thumbnail_filepath}.tmp")
        print_log(f"thumbnail:{movie_id}", f"writing thumbnail to '{thumbnail_filename}'")
        thumb_response = requests.get(full_size_thumb_url)
        if thumb_response.ok:
            with open(f"{thumbnail_filepath}.tmp", "wb") as movie_thumbnail:
                movie_thumbnail.write(thumb_response.content)
            if os.path.isfile(thumbnail_filepath):
                os.remove(thumbnail_filepath)
            os.rename(f"{thumbnail_filepath}.tmp", thumbnail_filepath)
        else:
            print_log(f"thumbnail:{movie_id}", "failed to retrieve video thumbnail")
            print_log(f"thumbnail:{movie_id}", f"API response returned status code {thumb_response.status_code}", LogLevel.VERBOSE)
    
    if args.write_live_chat:
        dl_live_chat(s, movie_id, movie_string, m_json["started_at"])

    if not args.skip_download:
        if downloading_format is not None:
            dl_m3u8_video(movie_id, movie_string, downloading_format)
        else:
            print_log(f"movie:{movie_id}", f"could not find video format '{args.format}'. to view all available formats, use --list-formats")
    else:
        print_log(f"movie:{movie_id}", f"skipping download")

def derive_media_playlists(movie_id, media_json, ps):
    base_url = None
    if not media_json["url_public"]:
        if ps:
            print_log(f"info:{movie_id}", "no public playlist found")
            # use the "private" API to get the a playlist url via auth
            view_res = None
            while True:
                priv_movie_response = ps.get(f"movies/{movie_id}/detail")
                if not priv_movie_response.ok:
                    print_log(f"info:{movie_id}", "failed to get movie information")
                    print_log(f"info:{movie_id}", f"private API response returned status code {priv_movie_response.status_code}", LogLevel.VERBOSE)
                    break
                pm_json = priv_movie_response.json()
                if "status" in pm_json and pm_json["status"] < 0:
                    print_log(f"info:{movie_id}", "failed to get movie information")
                    print_log(f"info:{movie_id}", f"private API body returned status code {pm_json['status']}: {pm_json['message']}", LogLevel.VERBOSE)
                    break
                if len(pm_json["data"]["items"]) > 0:
                    if pm_json["data"]["items"][0]["media"]["url"] != None:
                        base_url = pm_json["data"]["items"][0]["media"]["url"]
                        break
                    elif not view_res:
                        # check if there is a free watch available and if so use it
                        view_data = pm_json["data"]["items"][0]["views_limit"]
                        if view_data["has_permission"] and view_data["remain"] > 0:
                            # request free watch
                            view_res = ps.post("users/me/views-limit", json={"movie_id": movie_id})
                            if not view_res.ok:
                                print_log(f"info:{movie_id}", "failed to request watch for movie")
                                print_log(f"info:{movie_id}", f"private API response returned status code {view_res.status_code}", LogLevel.VERBOSE)
                                break
                            view_json = view_res.json()
                            if "status" in view_json and view_json["status"] < 0:
                                print_log(f"info:{movie_id}", "failed due to bad response from view limit endpoint")
                                print_log(f"info:{movie_id}", f"private API body returned status code {view_json['status']}: {view_json['message']}", LogLevel.VERBOSE)
                                break
                            print(view_json)
                            view_data = view_json["data"][0]
                            if view_data["has_permission"]:
                                # success, no break so detail request is made again
                                print_log(f"info:{movie_id}", f"using free watch, you have {view_data['remain']} watches remaining")
                            elif view_data["remain"] > 0:
                                # failure, but for some reason the API still says there are free watches available
                                print_log(f"info:{movie_id}", f"failed to get movie access, please report this issue to \'{ISSUES_URL}\'")
                                break
                            else:
                                # failure, ran out of free watches
                                print_log(f"info:{movie_id}", f"failed to get movie access, no free watches available")
                                break
                        else:
                            print_log(f"info:{movie_id}", f"failed to get movie access, no free watches available")
                            break
                    else:
                        print_log(f"info:{movie_id}", f"failed to get any playlist information, check that you have access to this livestream or report this issue at \'{ISSUES_URL}\'")
                        break
                else:
                    print_log(f"info:{movie_id}", f"failed to get any playlist information, check that you have access to this livestream or report this issue at \'{ISSUES_URL}\'")
                    break
        else:
            # loop through keys and attempt to find a url
            for url_type in media_json:
                if media_json[url_type]:
                    base_url = media_json[url_type]
                    break
            if not base_url:
                print_log(f"info:{movie_id}", "failed to get any playlist information, if you have access to this livestream try using --cookies")
    else:
        base_url = media_json["url_public"]
    
    if not base_url:
        return None
    print_log(f"playlist:{movie_id}", f"got playlist {base_url}", LogLevel.VERBOSE)
    # API may only give certain playlists by default, others can be derived for more complete information
    # these links might not actually exist, so downloading relies on values from the default playlist.m3u8
    ol_m = re.search(OLD_PL_HOST, base_url)
    nl_m = re.search(NEW_PL_HOST, base_url)
    gl_m = re.search(GAME_PL_HOST, base_url)
    # old hosting, barely any metadata provided, only one index
    if ol_m:
        v_id = ol_m.group("vid")
        print_log(f"playlist:{movie_id}", "older hosting found, format metadata will be sparse")
        media_json["url"] = base_url
    # new hosting, more metadata in playlists, two exposed index variants
    else:
        pl_map = None
        if nl_m:
            playlist_name = nl_m.group("pname")
            if playlist_name == "normal":
                pl_map = NORMAL_MAP
            elif playlist_name == "public":
                pl_map = PLAYLIST_MAP
            else:
                print_log(f"playlist:{movie_id}", f"index playlist not identified, please report this at \'{ISSUES_URL}\'")
                # default to assuming new playlist
                pl_map = PLAYLIST_MAP
        elif gl_m:
            playlist_name = gl_m.group("pname")
            print_log(f"playlist:{movie_id}", "game playlist found")
            pl_map = GAME_MAP
        else:
            print_log(f"playlist:{movie_id}", f"new playlist host found, please report this at \'{ISSUES_URL}\'")
            return None
        # fill JSON with playlist URLs assumed to exist based on index availability
        if pl_map:
            for pl_type in pl_map:
                if not pl_type in media_json or not media_json[pl_type]:
                    media_json[pl_type] = urllib.parse.urljoin(base_url, f"{pl_map[pl_type]}.m3u8")
    return media_json

def dl_m3u8_video(movie_id, movie_filename, m3u8_link):
    movie_path = os.path.join(args.directory, f"{movie_filename}")
    # don't re-download
    if os.path.isfile(f"{movie_path}.mp4") or (os.path.isfile(f"{movie_path}.ts") and args.skip_convert):
        print_log(f"movie:{movie_id}", f"already downloaded")
    else:
        if os.path.isfile(f"{movie_path}.ts"):
            print_log(f"movie:{movie_id}", f"already downloaded")
        # can't resume downloads (atm), so remove progress
        else:
            if os.path.isfile(f"{movie_path}.ts.tmp"):
                os.remove(f"{movie_path}.ts.tmp")
                for seg_file in os.listdir(args.directory):
                    if re.fullmatch(r"^" + f"{re.escape(movie_filename)}" + r"\.seg[0-9]{1,}$", seg_file):
                        os.remove(seg_file)
            
            # get necessary variables ready
            playlist_base = urllib.parse.urljoin(m3u8_link, ".")
            m3u8_text = requests.get(m3u8_link).text
            ts_list = [n for n in m3u8_text.splitlines() if n and not n.startswith("#")]
            ordered_ts_list = list(zip(ts_list, [n for n in range(len(ts_list))]))

            # run the downloader
            print_log(f"movie:{movie_id}", f"writing video to '{movie_filename}.ts'")
            stream_downloader = StreamDownloader(playlist_base)
            download_bar = DownloadBar(f"[movie:{movie_id}]", max=len(ts_list))
            stream_downloader.run(movie_filename, ordered_ts_list, download_bar)

            # if success, check if converting
            if stream_downloader.success:
                download_bar.finish()
                os.rename(f"{movie_path}.ts.tmp", f"{movie_path}.ts")
            else:
                print_log(f"movie:{movie_id}", f"failed to download")
                return
        if not args.skip_convert:
            mpeg_convert(os.path.join(args.directory, f"{movie_filename}"))
    if args.download_archive:
        with open(args.download_archive, "a") as archive_file:
            archive_file.write(f"{movie_id}\n")

def mpeg_convert(file_path):
    print_log("mpeg-convert", f"converting video to '{os.path.basename(file_path)}.mp4'")
    wait_for_file = 0
    # wait up to 30 seconds for processes (including this one) to release file
    while wait_for_file < 30:
        if os.path.isfile(f"{file_path}.ts"):
            break
        sleep(1)
        wait_for_file += 1
    if wait_for_file == 30:
        print_log("mpeg-convert", f"could not access file '{os.path.basename(file_path)}.ts'")
        return
    ffmpeg_list = ["ffmpeg", "-i", f"{file_path}.ts", "-acodec", "copy", "-vcodec", "copy", f"{file_path}.mp4"]
    try:
        ffmpeg_process = Popen(ffmpeg_list, stdout=PIPE, stderr=PIPE)
        stdout, stderr = ffmpeg_process.communicate()
    except Exception:
        print_log("mpeg-convert", "failure in executing ffmpeg")
        print_log("ffmpeg", f"stdout: {str(stdout)}\n\nstderr: {str(stderr)}", LogLevel.VERBOSE)
        return
    # don't remove .ts if .mp4 was not created
    if os.path.isfile(f"{file_path}.mp4"):
        os.remove(f"{file_path}.ts")

# based on parts of https://github.com/ytdl-org/youtube-dl/blob/master/youtube_dl/extractor/common.py
def get_m3u8_info(playlist_link):
    m3u8_info = []
    m3u8_text = requests.get(playlist_link).text
    print_log("get-m3u8-info", f"retrieving playlist from {playlist_link}", LogLevel.VERBOSE)
    media_details = None
    format_details = None
    for line in m3u8_text.splitlines():
        if line.startswith("#EXT-X-MEDIA:"):
            # parse media details
            media_details = parse_m3u8_attributes(line)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            # parse format details
            format_details = parse_m3u8_attributes(line)
        elif not line.startswith("#"):
            if line.endswith(".m3u8"):
                if format_details:
                    if not media_details:
                        print_log("get-m3u8-info", f"could not find media details for playlist '{line}', using format details", LogLevel.VERBOSE)
                        if "source" in line:
                            media_name = "Source"
                        elif "RESOLUTION" in format_details:
                            media_name = format_details["RESOLUTION"].split("x")[1] + "p"
                        else:
                            media_name = line.split("/")[0].split(".")[0]
                        media_details = {"NAME": media_name, "GROUP-ID": "", "TYPE": ""}
                    if not "FRAME-RATE" in format_details:
                        format_details["FRAME-RATE"] = ""
                    if not "RESOLUTION" in format_details:
                        format_details["RESOLUTION"] = ""
                    if not "CODECS" in format_details:
                        format_details["CODECS"] = ""
                else:
                    print_log("get-m3u8-info", f"could not find format details for playlist '{line}', please report this issue at \'{ISSUES_URL}\'")
                if format_details and media_details:
                    m3u8_info += [{"location": line, "media": media_details, "format": format_details}]
                    media_details = None
                    format_details = None
            else:
                print_log("get-m3u8-info", f"unexpected line in m3u8 file: '{line}'", LogLevel.VERBOSE)
    return m3u8_info

# https://github.com/ytdl-org/youtube-dl/blob/master/youtube_dl/utils.py#L5495
def parse_m3u8_attributes(attrib):
    info = {}
    for (key, val) in re.findall(r'(?P<key>[A-Z0-9-]+)=(?P<val>"[^"]+"|[^",]+)(?:,|$)', attrib):
        if val.startswith("\""):
            val = val[1:-1]
        info[key] = val
    return info

def print_formats(formats_list):
    print(f"{'NAME':<10} {'GROUP-ID':<8} {'RESOLUTION':<10} {'FPS':<4} {'TBR':<6} {'CODECS':<24}")
    print(f"{'-' * 10} {'-' * 8} {'-' * 10} {'-' * 4} {'-' * 6} {'-' * 24}")
    for format_settings in formats_list:
        print(f"{format_settings['media']['NAME']:<10} " +
        f"{format_settings['media']['GROUP-ID']:<8} " +
        f"{format_settings['format']['RESOLUTION']:<10} " +
        f"{format_settings['format']['FRAME-RATE']:<4} " +
        f"{str(int(float(format_settings['format']['BANDWIDTH']) / 1000))+'k':<6} " +
        f"{format_settings['format']['CODECS']:<24}")
        

def dl_live_chat(s, movie_id, movie_filename, started_at):
    live_chat_filename = f"{movie_filename}.live_chat.json"
    live_chat_filepath = os.path.join(args.directory, live_chat_filename)
    if os.path.isfile(live_chat_filepath):
        print_log(f"live-chat:{movie_id}", "already downloaded")
        return
    # no resuming downloads, so just remove the temp
    elif os.path.isfile(f"{live_chat_filepath}.tmp"):
        os.remove(f"{live_chat_filepath}.tmp")

    # read datetime object and remove utc offset for using in the chat API
    chat_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S%z")
    chat_dt = (chat_dt - chat_dt.utcoffset()).replace(tzinfo=None)
    chat_response = s.get(f"movies/{movie_id}/chats?from_created_at={chat_dt.isoformat()}.000Z&is_including_system_message=false")
    
    print_log(f"live-chat:{movie_id}", f"writing live chat to \'{live_chat_filename}\'")
    with open(f"{live_chat_filepath}.tmp", "w") as live_chat_file:
        # loop until blank response
        while chat_response.ok and len(chat_response.json()) > 0:
            last_post_time = datetime.strptime(chat_response.json()[-1]["posted_at"], "%Y-%m-%dT%H:%M:%S%z")
            last_post_time = (last_post_time - last_post_time.utcoffset()).replace(tzinfo=None)
            # stop if this is the same date as previously requested
            if last_post_time.isoformat() == chat_dt.isoformat():
                break
            for chat_line in chat_response.json():
                live_chat_file.write(f"{json.dumps(chat_line)}\n")
            # use timestamp of last chat post retrieved to fill next url
            chat_dt = last_post_time
            chat_response = s.get(f"movies/{movie_id}/chats?from_created_at={chat_dt.isoformat()}.000Z&is_including_system_message=false")
    if not chat_response.ok:
        print_log(f"live-chat:{movie_id}", f"unexpected ending with API response status code {chat_response.status_code}", LogLevel.VERBOSE)
    os.rename(f"{live_chat_filepath}.tmp", live_chat_filepath)

def create_priv_api_session(cookie_jar_path=None, cookie_jar=None):
    priv_session = sessions.BaseUrlSession(base_url=PRIVATE_API)
    
    if cookie_jar_path is not None:    
        cookie_jar = cookiejar.MozillaCookieJar(cookie_jar_path)
        try:
            cookie_jar.load()
        except:
            print_log(f"failed to load cookies file {cookie_jar_path}, continuing without cookies")
            return None

    # clean up the cookie jar and get necessary header values for private API
    session_headers = {}
    for c in cookie_jar:
        if c.domain != COOKIE_DOMAIN:
            cookie_jar.clear(c.domain, c.path, c.name)
        elif c.name == "access_token":
            session_headers["access-token"] = c.value
        elif c.name == "uuid":
            session_headers["uuid"] = c.value
    priv_session.headers = session_headers
    priv_session.cookies = cookie_jar
    return priv_session

def get_cookies_from_username_password(username, password):
    session = requests.Session()

    body = {
        "mail": username,
        "password": password,
    }

    response = session.post("https://www.openrec.tv/viewapp/v4/mobile/user/login", data=body)
    json_response = response.json()

    if json_response["status"] < 0:
        print_log("openrec", json_response["error_message"])
        sys.exit()

    return response.cookies

def print_log(component, message, level=LogLevel.BASIC):
    if level == LogLevel.VERBOSE and not args.verbose:
        return
    print(f"[{component}] {message}")

def get_arguments():
    parser.add_argument("--version", action="store_true", help="print version string and exit")
    parser.add_argument("-V", "--verbose", action="store_true", help="print debugging information")
    parser.add_argument("-d", "--directory", type=str, help="save directory (defaults to current)", default=os.getcwd())
    parser.add_argument("--download-archive", metavar="FILE", type=str, help="download only videos not listed in the archive file and record the IDs of downloaded videos")
    parser.add_argument("--write-info-json", action="store_true", help="write metadata to .info.json file")
    parser.add_argument("--write-thumbnail", action="store_true", help="write thumbnail to image file")
    parser.add_argument("--write-live-chat", action="store_true", help="write live chat comments to .live_chat.json file")
    parser.add_argument("-f", "--format", type=str, help="video format, specified by either NAME or GROUP-ID, or the keyword \'best\'", default="best")
    parser.add_argument("-F", "--list-formats", action="store_true", help="print available format details for a video and exit")
    parser.add_argument("--skip-download", action="store_true", help="do not download the video")
    parser.add_argument("--skip-convert", action="store_true", help="do not use ffmpeg to convert the MPEG-TS stream to MPEG-4")
    parser.add_argument("--cookies", metavar="COOKIES FILE", type=str, help="path to a Netscape format cookies file where cookies will be read from/written to")
    parser.add_argument("links", metavar="LINK", nargs="*", help="openrec channel or video link(s)/ids")
    parser.add_argument("-u", "--username", type=str, help="account's username to get cookies")
    parser.add_argument("-p", "--password", type=str, help="account's password to get cookies")
    return parser.parse_args()

def main():
    pub_api_session = sessions.BaseUrlSession(base_url=PUBLIC_API)
    priv_api_session = None
    if args.cookies:
        if os.path.isfile(args.cookies):
            priv_api_session = create_priv_api_session(cookie_jar_path=args.cookies)
        else:
            print_log("openrec-dl", f"could not find cookies file \'{args.cookies}\', continuing without cookies")
    elif args.username and args.password:
        cookies = get_cookies_from_username_password(args.username, args.password)
        priv_api_session = create_priv_api_session(cookie_jar=cookies)

    if args.version:
        print(VERSION_STRING)
        return
    if len(args.links) == 0:
        parser.print_usage()
    elif not os.path.isdir(args.directory):
        os.makedirs(args.directory)
    for link in args.links:
        # is openrec link
        openrec_m = re.search(OPENREC, link)
        id_m = re.search(VALID_LIVE_ID, link)
        if openrec_m:
            t = openrec_m.group("type")
            if t == "user":
                dl_channel(pub_api_session, priv_api_session, openrec_m.group("id"))
            elif t == "live":
                dl_movie(pub_api_session, priv_api_session, openrec_m.group("id"))
            else:
                print_log("openrec", f"unknown link type \'{t}\'")
        elif id_m:
            dl_movie(pub_api_session, priv_api_session, id_m.group("id"))
        else:
            print_log("openrec", f"invalid link or id \'{link}\'")

parser = argparse.ArgumentParser()
args = get_arguments()

if __name__ == "__main__":
    main()
