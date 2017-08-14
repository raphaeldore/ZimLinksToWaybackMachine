import sys
import os
import argparse
import pathlib
import re
import logging
import json
from typing import List

import requests

from datetime import datetime
from urllib.parse import urlparse
from collections import namedtuple
from urlextract import URLExtract

FORMAT = '%(asctime)s %(name)s %(levelname)-8s %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger("zl2wbm")

BASE_WEB_ARCHIVE_URL = "https://web.archive.org"
WAYBACK_API_URL = "https://pragma.archivelab.org"
IGNORED_HOSTS = ["web.archive.org", "archive.is", "web-beta.archive.org", "localhost"]

ArchivedUrl = namedtuple("ArchivedUrl", "original_url archived_url")

url_extractor = URLExtract()


class SaveLinkToWaybackMachineException(Exception):
    pass


def get_args():
    class IsValidZimNotebookAction(argparse.Action):
        def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values,
                     option_string=None):
            notebook_folder_path = values
            if not os.path.isfile(os.path.join(notebook_folder_path, "notebook.zim")):
                parser.error(
                    "{0} is not valid Zim notebook directory (it's missing the required file notebook.zim).".format(
                        notebook_folder_path))
            else:
                setattr(namespace, self.dest, notebook_folder_path)

    class LoggingAction(argparse.Action):
        LOG_LEVEL_STRINGS = ['critical', 'error', 'warning', 'info', 'debug']

        def __call__(self, parser, namespace, values, option_string=None):
            level_name = values
            setattr(namespace, self.dest, logging.getLevelName(level_name.upper()))

    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--zim-notebook-directory",
                        required=True,
                        type=str,
                        action=IsValidZimNotebookAction,
                        help="Full path to the Zim notebook you want to crawl and archive links.")

    parser.add_argument("-l", "--log-level",
                        help="Sets the logging level for the whole application.",
                        choices=LoggingAction.LOG_LEVEL_STRINGS,
                        action=LoggingAction,
                        default="DEBUG")

    return parser.parse_args()


def save_link_in_wayback_machine(url: str) -> str:
    url = url if '://' in url else 'http://' + url
    archived_url = None

    # Let's first check if a recent copy already exists in the wayback machine
    # Uses the API described here: https://archive.org/help/wayback_api.php
    logger.debug("Checking to see if a recent copy of {} already exists in Wayback Machine.".format(url))
    r = requests.get("http://archive.org/wayback/available?url=" + url)
    json_response = json.loads(r.content)
    archived_snapshots = json_response["archived_snapshots"]

    if archived_snapshots:
        closest_snapshot = archived_snapshots["closest"]

        current_date = datetime.now()
        closest_snapshot_timestamp = datetime.strptime(closest_snapshot["timestamp"], "%Y%m%d%H%M%S")

        delta = current_date - closest_snapshot_timestamp

        # If the latest snapshot is less than 2 weeks old, then we use that one.
        if delta.days < 14:
            logger.debug("A recent copy of {0} indeeds exists in the Wayback Machine (date = {1}). Using that.".format(url,
                                                                                                                   closest_snapshot_timestamp.strftime("%Y-%m-%d %H:%M:%S")))
            archived_url = closest_snapshot["url"]
    else:
        logger.debug("No recent copy of {} exists in the Wayback Machine. Will create new archive.".format(url))

    if not archived_url:
        r = requests.get('http://web.archive.org/save/%s' % url)

        if 'X-Archive-Wayback-Runtime-Error' in r.headers:
            raise SaveLinkToWaybackMachineException(r.headers['X-Archive-Wayback-Runtime-Error'])

        if 'x-archive-wayback-liveweb-error' in r.headers:
            raise SaveLinkToWaybackMachineException(r.headers['x-archive-wayback-liveweb-error'])

        # content-location should look something like this: /web/20170813163039/http://www.google.ca/
        archived_url = BASE_WEB_ARCHIVE_URL + r.headers.get("content-location", url)

    logger.debug("{0} archived to {1}".format(url, archived_url))

    return archived_url


def protect_string_metacharacters(s: str) -> str:
    """
    Protect Metacharacters in a string
    (add a \\ before)
    :param s: string
    :returns: protected string
    """
    s = re.sub('\&', '\\\&', s)
    s = re.sub('\[', '\\\[', s)
    s = re.sub('\]', '\\\]', s)
    s = re.sub('\|', '\\\|', s)
    s = re.sub('\?', '\\\?', s)

    return s


def archive_links(urls: List[str]) -> List[ArchivedUrl]:
    archived_urls = []
    for url in urls:
        parsed_url = urlparse(url)

        if parsed_url and parsed_url.hostname not in IGNORED_HOSTS:
            try:
                logger.info("Archiving: {0}".format(url))
                archived_urls.append(ArchivedUrl(original_url=url, archived_url=save_link_in_wayback_machine(url)))
            except (requests.HTTPError, json.JSONDecodeError) as e:
                logger.info("There was an error while archiving the following URL: {0}. Skipping it.".format(url))
                logger.exception(e)

    return archived_urls


def get_urls_to_archive_from_text(text: str) -> List[str]:
    urls_to_archive = []

    for url in url_extractor.find_urls(text, only_unique=True):
        # find_urls returns this as a url for some weird reason: [[https://test.org/|Archive]]
        # So we extract the url from that. If the url doesn't contains [[ ]] or | then nothing happens.
        url = url.lstrip('[[').rstrip(']]').split('|', 1)[0]

        parsed_url = urlparse(url)
        if parsed_url.hostname and parsed_url.hostname not in IGNORED_HOSTS:
            # For example:
            # (:?(?:https://google.com\/)|(?:\[\[https\:\/\/google\.com\/\|.*\]\])){1}\s\(\[\[.*\|Archive\]\]\)
            # Matches:
            # https://google.com ([[https://web.archive.org/web/20170724012307/https://google.com|Archive]])
            # [[https://google.com|Patate]] ([[https://web.archive.org/web/20170724012307/http:s//google.com|Archive]])
            regex = "(:?(:?{url})|(?:\[\[{url}\|.*\]\])){{1}}\s\(\[\[.*\|Archive\]\]\)".format(url=re.escape(url))
            link_archived = re.compile(regex)

            # if there is an "(Archive)" link next to the link, then it means it has already been archived.
            if not link_archived.search(text):
                urls_to_archive.append(url)

    return urls_to_archive


def edit_text(text: str, archived_urls: List[ArchivedUrl]) -> str:
    """
    >>> edit_text("Normal link: http://google.com", [ArchivedUrl(original_url='http://google.com', archived_url='http://google.archive')])
    'Normal link: http://google.com ([[http://google.archive|Archive]])'
    >>> edit_text("Integrated link: [[http://google.com|Google]]", [ArchivedUrl(original_url='http://google.com', archived_url='http://google.archive')])
    'Integrated link: [[http://google.com|Google]] ([[http://google.archive|Archive]])'
    >>> edit_text("No links in this text!", [ArchivedUrl(original_url='http://google.com', archived_url='http://google.archive')])
    'No links in this text!'

    :param text:
    :param archived_urls:
    :return:
    """
    for archived_url in archived_urls:
        # regex = "(:?(:?{url})|(?:\[\[{url}\|.*\]\])){{1}}".format(url=re.escape(archived_url.original_url))

        # normal_link_regex = "{url}"
        # matches: BOL or whitespace {url} EOL or whitespace. Ex: http://google.com
        normal_link_regex = re.compile("(?:(?<=\s)|(?<=^)){url}(?=\s|$)".format(url=archived_url.original_url))
        # matches: BOL or whitespace [{url}|... ]] whitespace or EOL. Ex: [[http://google.com|Google]]
        zim_integrated_link_regex = re.compile(
            "(?:(?<=\s)|(?<=^))\[\[{url}\|[^\[]*\]\](?=\s|$)".format(url=archived_url.original_url))

        integrated_link_match = zim_integrated_link_regex.search(text)
        normal_link_match = normal_link_regex.search(text)
        if integrated_link_match:
            effective_pattern = integrated_link_match.group(0)
        elif normal_link_match:
            effective_pattern = archived_url.original_url
        else:
            continue

        new_url = effective_pattern + " ([[{0}|Archive]])".format(archived_url.archived_url)
        text = re.sub(re.escape(effective_pattern), new_url, text)

    return text


def crawl_notebook_and_archive_links(zim_notebook_directory: str) -> None:
    text_files = pathlib.Path(zim_notebook_directory).rglob('*.txt')

    for text_file in text_files:
        try:
            file_contents = text_file.read_text(encoding='utf8')
            first_line = file_contents.split('\n', 1)[0]
        except UnicodeDecodeError:
            logger.error("Text file \"{0}\" got UnicodeDecodeError. Skipping.".format(text_file), file=sys.stderr)
            continue

        if first_line != "Content-Type: text/x-zim-wiki":
            return

        logger.debug("{0} is a zim wiki file!".format(text_file))
        urls_to_archive = get_urls_to_archive_from_text(file_contents)

        logger.info("{0} URLs to archive: {1}".format(text_file, ', '.join(urls_to_archive)))

        archived_urls = archive_links(urls_to_archive)

        logger.info("{0} Archived URLs: {1}".format(text_file, ', '.join(map(str, archived_urls))))

        new_file_contents = edit_text(file_contents, archived_urls)

        print(new_file_contents)

        # if new_file_contents != file_contents:
        #    text_file.write_text(new_file_contents)


def main():
    args = get_args()

    logger.setLevel(logging.getLevelName(args.log_level))

    crawl_notebook_and_archive_links(args.zim_notebook_directory)


if __name__ == "__main__":
    main()
