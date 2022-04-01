# OPENREC-dl

As the name might suggest, this is a python downloader for the site [OPENREC.tv](https://www.openrec.tv/). The arguments and output are as you might expect if you've used a downloader like youtube-dl before, but just to be sure they are outlined below. Developed using Python 3.8.5, tested working on Python 3.7.3, may not work on older versions.

## Install

```
pip3 install -r requirements.txt
```

## Usage

```
python3 openrec-dl.py [ARGUMENTS] LINK/ID [LINK/ID...]
```

## Arguments

```
-h, --help                    show this help message and exit
--version                     print version string and exit
-V, --verbose                 print debugging information
-d, --directory DIRECTORY     save directory (defaults to current)
--download-archive FILE       download only videos not listed in the archive
                              file and record the IDs of downloaded videos
--write-info-json             write metadata to .info.json file
--write-thumbnail             write thumbnail to image file
--write-live-chat             write live chat comments to .live_chat.json file
-f, --format FORMAT           video format, specified by either NAME or
                              GROUP-ID, or the keyword "best" (defaults is "best",
                              retrieves either Source quality or highest bitrate)
-F, --list-formats            print available format details for a video and exit
--skip-download               do not download the video
--skip-convert                do not use ffmpeg to convert the MPEG-TS stream to MPEG-4
-u, --username                username/email address for an openrec.tv account
-p, --password                password for an openrec.tv account
--cookies                     a Netscape format cookies file, may make available some
                              downloads that are otherwise unavailable
```

## Authentication

You can use either a Netscape cookie file (with `--cookies`) or a set of credentials (with `--username` and `--password`) to authenticate with openrec-dl. If have a free account, the downloader will attempt to use the ability to view two videos free per day, but only if it cannot get around this restriction already. If you have a premium account you should encounter no limits to what videos the downloader can access.
