#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta
from enum import Enum
import gevent
from gevent import monkey
monkey.patch_all()
from gevent.pool import Pool
import glob
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

VERSION_STRING="2021.03.18"

OPENREC=r'^(?:https?:\/\/)?(?:www\.)?openrec\.tv\/(?P<type>[^\/]+?)\/(?P<id>[^\/]+)$'
VALID_LIVE_ID=r'^(?P<id>[a-z0-9]+?)$'
FULL_SIZE_IMG=r'\.w[0-9]{1,}\.ttl[0-9]{1,}\.(?P<ext>[a-z]{1,4})\?'
FULL_SIZE_REP=r'.\g<ext>?q=100&quality=100&'
CLEAN_FILENAME_KINDA=r'[^\w\-_\. \[\]\(\)]'
API="https://public.openrec.tv/external/api/v5/"

class LogLevel(Enum):
    BASIC=1
    VERBOSE=2

class DownloadBar(IncrementalBar):
    suffix="%(percent).1f%% ETA %(time_remaining)s"
    @property
    def time_remaining(self):
        ts = self.eta_td.total_seconds()
        m = ts % 60
        s = ts / 60
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

def dl_channel(s, channel_id):
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

    # use /channels json if /search-users json doesn't exist for some reason
    c_json = c_init_json
    if search_response.ok:
        found_full_json = False
        for user_result in search_response.json():
            if user_result["name"] == c_init_json["name"]:
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
        print_log(f"info:{channel_id}", f"writing channel information to '{info_filename}'")
        with open(os.path.join(args.directory, f"{info_filename}.tmp"), "w") as channel_info:
            channel_info.write(json.dumps(c_json))
        os.rename(os.path.join(args.directory, f"{info_filename}.tmp"), os.path.join(args.directory, info_filename))
    
    if args.write_thumbnail:
        # icon (avatar)
        full_size_icon_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, c_json["icon_image_url"])
        icon_format = urllib.parse.parse_qs(full_size_icon_url)["format"][0]
        icon_filename = f"{channel_string}-icon.{icon_format}"
        print_log(f"icon:{channel_id}", f"writing channel icon to '{icon_filename}'")
        icon_response = requests.get(full_size_icon_url)
        if icon_response.ok:
            with open(os.path.join(args.directory, f"{icon_filename}.tmp"), "wb") as channel_icon:
                channel_icon.write(icon_response.content)
            os.rename(os.path.join(args.directory, f"{icon_filename}.tmp"), os.path.join(args.directory, icon_filename))
        else:
            print_log(f"icon:{channel_id}", "failed to retrieve channel icon")
            print_log(f"icon:{channel_id}", f"API response returned status code {icon_response.status_code}", LogLevel.VERBOSE)
        
        # cover (banner)
        full_size_cover_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, c_json["cover_image_url"])
        cover_format = urllib.parse.parse_qs(full_size_cover_url)["format"][0]
        cover_filename = f"{channel_string}-cover.{cover_format}"
        print_log(f"cover:{channel_id}", f"writing channel cover to '{cover_filename}'")
        cover_response = requests.get(full_size_icon_url)
        if cover_response.ok:
            with open(os.path.join(args.directory, f"{cover_filename}.tmp"), "wb") as channel_cover:
                channel_cover.write(cover_response.content)
            os.rename(os.path.join(args.directory, f"{cover_filename}.tmp"), os.path.join(args.directory, cover_filename))
        else:
            print_log(f"icon:{channel_id}", "failed to retrieve channel cover")
            print_log(f"icon:{channel_id}", f"API response returned status code {cover_response.status_code}", LogLevel.VERBOSE)

    movie_list_response = s.get(f"movies?channel_ids={channel_id}").json()
    for movie_index in range(len(movie_list_response)):
        print_log(f"channel:{channel_id}", f"downloading video {movie_index + 1} of {len(movie_list_response)}")
        dl_movie(s, movie_list_response[movie_index]["id"])

def dl_movie(s, movie_id):
    # check if we can skip
    if args.download_archive:
        if os.path.isfile(args.download_archive):
            with open(args.download_archive, "r") as archive_file:
                skip_ids = archive_file.read().splitlines()
            if movie_id in skip_ids:
                print_log(f"movie:{movie_id}", "already recorded in archive")
                return

    # get video data and check validity
    movie_response = s.get(f"movies/{movie_id}")
    if not movie_response.ok:
        print_log(f"info:{movie_id}", "failed to get movie information")
        print_log(f"info:{movie_id}", f"API response returned status code {movie_response.status_code}", LogLevel.VERBOSE)
        return
    m_json = movie_response.json()
    if "status" in m_json:
        print_log(f"info:{movie_id}", "failed to get movie information")
        print_log(f"info:{movie_id}", f"API body returned status code {m_json['status']}: {m_json['message']}")
        return
    
    # string to use in output video names
    movie_string = re.sub(CLEAN_FILENAME_KINDA, "_", f"{m_json['title']} [{m_json['id']}]").strip()

    # remove ad info because who cares about ads
    m_json.pop("ad")

    # api only gives public m3u8 by default. we dont care :^)
    # derive m3u8 link from json[media][url_public]
    # these links might not actually exist, so downloading relies on values from playlist.m3u8
    m_json["media"]["url"] = urllib.parse.urljoin(m_json["media"]["url_public"], "playlist.m3u8")
    m_json["media"]["url_audio"] = urllib.parse.urljoin(m_json["media"]["url_public"], "aac.m3u8")
    m_json["media"]["url_source"] = urllib.parse.urljoin(m_json["media"]["url_public"], "chunklist_source/chunklist.m3u8")
    m_json["media"]["url_low_latency"] = urllib.parse.urljoin(m_json["media"]["url_public"], "chunklist_low/chunklist.m3u8")
    m_json["media"]["url_ull"] = urllib.parse.urljoin(m_json["media"]["url_public"], "chunklist_144p/chunklist.m3u8")

    formats_list = get_m3u8_info(m_json["media"]["url"])

    if args.list_formats:
        print_log(f"movie:{movie_id}", f"available formats:")
        print_formats(formats_list)
        return
    
    downloading_format = None
    for format_settings in formats_list:
        if args.format in {format_settings["media"]["NAME"], format_settings["media"]["GROUP-ID"]}:
            downloading_format = urllib.parse.urljoin(m_json["media"]["url_public"], format_settings["location"])

    if args.write_info_json:
        info_filename = f"{movie_string}.info.json"
        print_log(f"info:{movie_id}", f"writing video information to '{info_filename}'")
        with open(os.path.join(args.directory, f"{info_filename}.tmp"), "w") as movie_info:
            movie_info.write(json.dumps(m_json))
        os.rename(os.path.join(args.directory, f"{info_filename}.tmp"), os.path.join(args.directory, info_filename))
    
    if args.write_thumbnail:
        full_size_thumb_url = re.sub(FULL_SIZE_IMG, FULL_SIZE_REP, m_json["thumbnail_url"])
        thumbnail_format = urllib.parse.parse_qs(full_size_thumb_url)["format"][0]
        thumbnail_filename = f"{movie_string}.{thumbnail_format}"
        print_log(f"thumbnail:{movie_id}", f"writing thumbnail to '{thumbnail_filename}'")
        thumb_response = requests.get(full_size_thumb_url)
        if thumb_response.ok:
            with open(os.path.join(args.directory, f"{thumbnail_filename}.tmp"), "wb") as movie_thumbnail:
                movie_thumbnail.write(thumb_response.content)
            os.rename(os.path.join(args.directory, f"{thumbnail_filename}.tmp"), os.path.join(args.directory, thumbnail_filename))
        else:
            print_log(f"thumbnail:{movie_id}", "failed to retrieve video thumbnail")
            print_log(f"thumbnail:{movie_id}", f"API response returned status code {thumb_response.status_code}", LogLevel.VERBOSE)
    
    if args.write_live_chat:
        dl_live_chat(s, movie_id, movie_string, m_json["started_at"])

    if not args.skip_download:
        if downloading_format:
            dl_m3u8_video(movie_id, movie_string, downloading_format)
        else:
            print_log(f"movie:{movie_id}", f"could not find video format '{args.format}'. to view all available formats, use --list-formats")
    else:
        print_log(f"movie:{movie_id}", f"skipping download")

def dl_m3u8_video(movie_id, movie_filename, m3u8_link):
    movie_path = os.path.join(args.directory, f"{movie_filename}")
    # don't re-download
    if os.path.isfile(f"{movie_path}.ts") or os.path.isfile(f"{movie_path}.mp4"):
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
            if not args.skip_convert:
                mpeg_convert(os.path.join(args.directory, f"{movie_filename}"))
        else:
            print_log(f"movie:{movie_id}", f"failed to download")
            return
    if args.download_archive:
        with open(args.download_archive, "a") as archive_file:
            archive_file.write(f"{movie_id}\n")

def mpeg_convert(file_path):
    print_log("mpeg-convert", f"converting video to '{movie_filename}.mp4'")
    ffmpeg_list = ["ffmpeg", "-i", f"{file_path}.ts", "-acodec", "copy", "-vcodec", "copy", f"{file_path}.mp4"]
    try:
        ffmpeg_process = Popen(ffmpeg_list, stdout=PIPE, stderr=PIPE)
        stdout, stderr = ffmpeg_process.communicate()
    except Exception:
        print_log("mpeg-convert", "failure in executing ffmpeg")
        return
    os.remove(f"{file_path}.ts")

# based on parts of https://github.com/ytdl-org/youtube-dl/blob/master/youtube_dl/extractor/common.py
def get_m3u8_info(playlist_link):
    m3u8_info = []
    m3u8_text = requests.get(playlist_link).text
    media_details = None
    format_details = None
    for line in m3u8_text.splitlines():
        if line.startswith("#EXT-X-MEDIA:"):
            media_details = parse_m3u8_attributes(line)
            #media_type, group_id, name = media.get("TYPE"), media.get("GROUP-ID"), media.get("NAME")
        elif line.startswith("#EXT-X-STREAM-INF:"):
            format_details = parse_m3u8_attributes(line)
        elif not line.startswith("#"):
            if line.endswith(".m3u8"):
                if not media_details:
                    print_log("get-m3u8-info", f"could not find media details for stream with format '{str(format_details)}'", LogLevel.VERBOSE)
                elif not format_details:
                    print_log("get-m3u8-info", f"could not find format details for stream with media '{str(media_details)}'", LogLevel.VERBOSE)
                else:
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
    print(f"{'NAME':<7} {'GROUP-ID':<8} {'RESOLUTION':<10} {'FPS':<4} {'TBR':<6} {'CODECS':<24}")
    print(f"{'-' * 7} {'-' * 8} {'-' * 10} {'-' * 4} {'-' * 6} {'-' * 24}")
    for format_settings in formats_list:
        print(f"{format_settings['media']['NAME']:<7} " +
        f"{format_settings['media']['GROUP-ID']:<8} " +
        f"{format_settings['format']['RESOLUTION']:<10} " +
        f"{format_settings['format']['FRAME-RATE']:<4} " +
        f"{str(int(float(format_settings['format']['BANDWIDTH']) / 1000))+'k':<6} " +
        f"{format_settings['format']['CODECS']:<24}")

def dl_live_chat(s, movie_id, movie_filename, started_at):
    live_chat_filename = os.path.join(args.directory, f"{movie_filename}.live_chat.json")
    if os.path.isfile(live_chat_filename):
        print_log(f"live-chat:{movie_id}", "already downloaded")
        return
    # no resuming downloads, so just remove the temp
    elif os.path.isfile(f"{live_chat_filename}.tmp"):
        os.remove(f"{live_chat_filename}.tmp")

    # read datetime object and remove utc offset for using in the chat API
    chat_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S%z")
    chat_dt = (chat_dt - chat_dt.utcoffset()).replace(tzinfo=None)
    chat_response = s.get(f"movies/{movie_id}/chats?from_created_at={chat_dt.isoformat()}.000Z&is_including_system_message=false")
    
    print_log(f"live-chat:{movie_id}", f"writing live chat to {movie_filename}.live_chat.json")
    with open(f"{live_chat_filename}.tmp", "w") as live_chat_file:
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
    os.rename(f"{live_chat_filename}.tmp", live_chat_filename)

def print_log(component, message, level=LogLevel.BASIC):
    if level == LogLevel.VERBOSE and not args.verbose:
        return
    print(f"[{component}] {message}")

def get_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true", help="print version string and exit")
    parser.add_argument("-V", "--verbose", action="store_true", help="print debugging information")
    parser.add_argument("-d", "--directory", type=str, help="save directory (defaults to current)", default=os.getcwd())
    parser.add_argument("--download-archive", metavar="FILE", type=str, help="download only videos not listed in the archive file and record the IDs of downloaded videos")
    parser.add_argument("--write-info-json", action="store_true", help="write metadata to .info.json file")
    parser.add_argument("--write-thumbnail", action="store_true", help="write thumbnail to image file")
    parser.add_argument("--write-live-chat", action="store_true", help="write live chat comments to .live_chat.json file")
    parser.add_argument("-f", "--format", type=str, help="video format, specified by either NAME or GROUP-ID (defaults to Source)", default="Source")
    parser.add_argument("-F", "--list-formats", action="store_true", help="print available format details for a video and exit")
    parser.add_argument("--skip-download", action="store_true", help="do not download the video")
    parser.add_argument("--skip-convert", action="store_true", help="do not use ffmpeg to convert the MPEG-TS stream to MPEG-4")
    parser.add_argument("links", metavar="LINK", nargs="*", help="openrec channel or video link(s)/ids")
    return parser.parse_args()

def main():
    api_session = sessions.BaseUrlSession(base_url=API)
    if args.version:
        print(VERSION_STRING)
        return
    if len(args.links) > 0 and not os.path.isdir(args.directory):
        os.makedirs(args.directory)
    for link in args.links:
        # is openrec link
        openrec_m = re.search(OPENREC, link)
        id_m = re.search(VALID_LIVE_ID, link)
        if openrec_m:
            t = openrec_m.group("type")
            if t == "user":
                dl_channel(api_session, openrec_m.group("id"))
            elif t == "live":
                dl_movie(api_session, openrec_m.group("id"))
            else:
                print_log("openrec", f"unknown link type \'{t}\'")
        elif id_m:
            dl_movie(api_session, id_m.group("id"))
        else:
            print_log("openrec", f"invalid link or id \'{link}\'")

args = get_arguments()

if __name__ == "__main__":
    main()
