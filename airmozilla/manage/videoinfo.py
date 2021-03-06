import re
import subprocess
import tempfile
import shutil
import os
import time
import sys
import traceback
import urlparse
import glob
import stat

import requests

from django.core.cache import cache
from django.conf import settings
from django.template.defaultfilters import filesizeformat
from django.db.models import Q
from django.core.files import File

from airmozilla.main.models import Event, VidlySubmission, Picture
from airmozilla.base.helpers import show_duration
from airmozilla.manage import vidly

REGEX = re.compile('Duration: (\d+):(\d+):(\d+).(\d+)')


def _download_file(url, local_filename):
    # NOTE the stream=True parameter
    r = requests.get(url, stream=True)
    with open(local_filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:  # filter out keep-alive new chunks
                f.write(chunk)
                f.flush()


def fetch_duration(
    event, save=False, save_locally=False, verbose=False, use_https=True,
):
    # The 'filepath' is only not None if you passed 'save_locally' as true
    video_url, filepath = _get_video_url(
        event,
        use_https,
        save_locally,
        verbose=verbose
    )

    # Some videos might return a 200 OK on a HEAD but are corrupted
    # and contains nothing
    if not save_locally:
        assert '://' in video_url
        head = requests.head(video_url)
        if head.headers.get('Content-Length') == '0':
            # corrupt file!
            raise AssertionError(
                '%s has a 0 byte Content-Length' % video_url
            )
        if head.headers.get('Content-Type', '').startswith('text/html'):
            # Not a URL to an actual file!
            raise AssertionError(
                '%s is a text/html document' % video_url
            )

    try:
        ffmpeg_location = getattr(
            settings,
            'FFMPEG_LOCATION',
            'ffmpeg'
        )
        command = [
            ffmpeg_location,
            '-i',
            video_url,
        ]
        if verbose:  # pragma: no cover
            print ' '.join(command)

        t0 = time.time()
        out, err = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        ).communicate()
        t1 = time.time()

        if verbose:  # pragma: no cover
            print "Took", t1 - t0, "seconds to extract duration information"

        matches = REGEX.findall(err)
        if matches:
            found, = matches
            hours = int(found[0])
            minutes = int(found[1])
            minutes += hours * 60
            seconds = int(found[2])
            seconds += minutes * 60
            if save:
                event.duration = seconds
                event.save()
            if verbose:  # pragma: no cover
                print show_duration(seconds, include_seconds=True)
            return seconds
        elif verbose:  # pragma: no cover
            print "No Duration output. Error:"
            print err
    finally:
        if save_locally:
            if os.path.isfile(filepath):
                shutil.rmtree(os.path.dirname(filepath))


def fetch_screencapture(
    event, save=False, save_locally=False, verbose=False, use_https=True,
    import_=True,
):
    assert event.duration, "no duration"
    video_url, filepath = _get_video_url(
        event,
        use_https,
        save_locally,
        verbose=verbose,
    )

    if import_:
        save_dir = tempfile.mkdtemp('screencaptures-%s' % event.id)
    else:
        # Instead of importing we're going to put them in a directory
        # that does NOT get deleted when it has created the screecaps.
        save_dir = os.path.join(
            tempfile.gettempdir(),
            settings.SCREENCAPTURES_TEMP_DIRECTORY_NAME
        )
        if not os.path.isdir(save_dir):
            os.mkdir(save_dir)
        directory_name = '%s_%s' % (event.id, event.slug)
        save_dir = os.path.join(save_dir, directory_name)
        if not os.path.isdir(save_dir):
            os.mkdir(save_dir)

    def format_time(seconds):
        m = seconds / 60
        s = seconds % 60
        h = m / 60
        m = m % 60
        return '%02d:%02d:%02d' % (h, m, s)

    try:
        if verbose:  # pragma: no cover
            print "Video duration:",
            print show_duration(event.duration, include_seconds=True)

        ffmpeg_location = getattr(
            settings,
            'FFMPEG_LOCATION',
            'ffmpeg'
        )
        incr = float(event.duration) / settings.SCREENCAPTURES_NO_PICTURES
        seconds = 0
        t0 = time.time()
        number = 0
        output_template = os.path.join(save_dir, 'screencap-%02d.jpg')
        all_out = []
        all_err = []
        while seconds < event.duration:
            number += 1
            output = output_template % number
            command = [
                ffmpeg_location,
                '-ss',
                format_time(seconds),
                '-i',
                video_url,
                '-vframes',
                '1',
                output,
            ]
            if verbose:  # pragma: no cover
                print ' '.join(command)
            out, err = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            ).communicate()
            all_out.append(out)
            all_err.append(err)
            seconds += incr
        t1 = time.time()

        files = _get_files(save_dir)
        if verbose:  # pragma: no cover
            print "Took", t1 - t0, "seconds to extract", len(files), "pictures"

        if import_:
            if verbose and not files:  # pragma: no cover
                print "No output. Error:"
                print '\n'.join(all_err)
            created = _import_files(event, files)
            if verbose:  # pragma: no cover
                print "Created", created, "pictures"
                # end of this section, so add some margin
                print "\n"
            return created
        else:
            if verbose:  # pragma: no cover
                print "Created Temporary Directory", save_dir
                print '\t' + '\n\t'.join(os.listdir(save_dir))
            return len(files)
    finally:
        if save_locally:
            if os.path.isfile(filepath):
                shutil.rmtree(os.path.dirname(filepath))
        if os.path.isdir(save_dir) and import_:
            shutil.rmtree(save_dir)


def _get_files(directory):
    filenames = []
    for filename in glob.glob(os.path.join(directory, 'screencap*.jpg')):
        size = os.stat(filename)[stat.ST_SIZE]
        if size > 0:
            filenames.append(filename)

    return filenames


def _get_video_url(event, use_https, save_locally, verbose=False):
    if 'Vid.ly' in event.template.name:
        assert event.template_environment.get('tag'), "No Vid.ly tag value"

        token_protected = event.privacy != Event.PRIVACY_PUBLIC
        hd = False
        qs = (
            VidlySubmission.objects
            .filter(event=event)
            .filter(tag=event.template_environment['tag'])
        )

        for submission in qs.order_by('-submission_time')[:1]:
            hd = submission.hd
            token_protected = submission.token_protection

        tag = event.template_environment['tag']
        video_url = 'https://vid.ly/%s?content=video&format=' % tag
        if hd:
            video_url += 'hd_mp4'
        else:
            video_url += 'mp4'

        if token_protected:
            video_url += '&token=%s' % vidly.tokenize(tag, 60)
    elif 'Ogg Video' in event.template.name:
        assert event.template_environment.get('url'), "No Ogg Video url value"
        video_url = event.template_environment['url']
    else:
        raise AssertionError("Not valid template")

    response = requests.head(video_url)
    _count = 0
    while response.status_code in (302, 301):
        video_url = response.headers['Location']
        response = requests.head(video_url)
        _count += 1
        if _count > 5:
            # just too many times
            break

    response = requests.head(video_url)
    assert response.status_code == 200, response.status_code
    if verbose:  # pragma: no cover
        if response.headers['Content-Length']:
            print "Content-Length:",
            print filesizeformat(int(response.headers['Content-Length']))

    if not use_https:
        video_url = video_url.replace('https://', 'http://')

    if save_locally:
        # store it in a temporary location
        dir_ = tempfile.mkdtemp('videoinfo')
        if 'Vid.ly' in event.template.name:
            filepath = os.path.join(dir_, '%s.mp4' % tag)
        else:
            filepath = os.path.join(
                dir_,
                os.path.basename(urlparse.urlparse(video_url).path)
            )
        t0 = time.time()
        _download_file(video_url, filepath)
        t1 = time.time()
        if verbose:  # pragma: no cover
            seconds = int(t1 - t0)
            print "Took", show_duration(seconds, include_seconds=True),
            print "to download"
        video_url = filepath
    else:
        filepath = None

    return video_url, filepath


def _import_files(event, files):
    created = 0
    # We sort and reverse by name so that the first instance
    # that is created is the oldest one.
    # That way, when you look at the in the picture gallery
    # (which is sorted by ('event', '-created')) they appear in
    # correct chronological order.
    for i, filepath in enumerate(reversed(sorted(files))):
        with open(filepath) as fp:
            Picture.objects.create(
                file=File(fp),
                notes="Screencap %d" % (len(files) - i,),
                event=event,
            )
            created += 1
    return created


def _fetch(
    qs,
    transform_function,
    max_=10,
    order_by='?',
    verbose=False,
    dry_run=False,
    save_locally=False,
    save_locally_some=False,
    **kwargs
):

    total_count = qs.count()
    if verbose:  # pragma: no cover
        print total_count, "events to process"
        print
    count = success = skipped = 0

    cache_key = 'videoinfo_quarantined' + transform_function.func_name
    quarantined = cache.get(cache_key, {})
    if quarantined:
        skipped += len(quarantined)
        if verbose:  # pragma: no cover
            print "Deliberately skipping"
            for e in Event.objects.filter(id__in=quarantined.keys()):
                print "\t%r (%s)" % (e.title, quarantined[e.id])

        qs = qs.exclude(id__in=quarantined.keys())

    for event in qs.order_by(order_by)[:max_ * 2]:
        if verbose:  # pragma: no cover
            print "-" * 80
            print "Event: %r, (privacy:%s slug:%s)" % (
                event.title,
                event.get_privacy_display(),
                event.slug,
            )
            if event.template_environment.get('tag'):
                print "Vid.ly tag:",
                print event.template_environment.get('tag')
            elif event.template_environment.get('url'):
                print "Ogg URL:",
                print event.template_environment.get('url')

        if (
            not (
                event.template_environment.get('tag')
                or
                event.template_environment.get('url')
            )
        ):
            if verbose:  # pragma: no cover
                print "No Vid.ly Tag or Ogg URL!"
            quarantined[event.id] = "No Vid.ly Tag or Ogg URL"
            skipped += 1
            continue

        count += 1
        try:
            use_https = True
            if save_locally_some:
                # override save_locally based on the type of event
                save_locally = event.privacy != Event.PRIVACY_PUBLIC
                # then this is not necessary
                use_https = save_locally

            transform_function(
                event,
                save=not dry_run,
                save_locally=save_locally,
                use_https=use_https,
                verbose=verbose,
                **kwargs
            )
            success += 1

        except AssertionError:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            print ''.join(traceback.format_tb(exc_traceback))
            print exc_type, exc_value
            # put it away for a while
            quarantined[event.id] = exc_value
            cache.set(cache_key, quarantined, 60 * 60)

        if count >= max_:
            break

    if verbose:  # pragma: no cover
        print "Processed", count,
        print '(%d successfully)' % success,
        print '(%d skipped)' % skipped
        print total_count - count, "left to go"


def fetch_durations(**kwargs):
    """this can be called by a cron job that will try to fetch
    duration for as many events as it can."""

    template_name_q = (
        Q(template__name__icontains='Vid.ly') |
        Q(template__name__icontains='Ogg Video')
    )
    qs = (
        Event.objects
        .filter(duration__isnull=True)
        .filter(template_name_q)
        .exclude(status=Event.STATUS_REMOVED)
    )
    _fetch(
        qs,
        fetch_duration,
        **kwargs
    )


def fetch_screencaptures(**kwargs):
    """this can be called by a cron job that will try to fetch
    duration for as many events as it can."""

    template_name_q = (
        Q(template__name__icontains='Vid.ly') |
        Q(template__name__icontains='Ogg Video')
    )
    qs = (
        Event.objects
        .filter(duration__isnull=False)
        .filter(template_name_q)
        .exclude(status=Event.STATUS_REMOVED)
    )
    qs = qs.exclude(
        id__in=Picture.objects.filter(event__isnull=False).values('event_id')
    )
    _fetch(
        qs,
        fetch_screencapture,
        **kwargs
    )


def import_screencaptures(verbose=False):
    dir_ = os.path.join(
        tempfile.gettempdir(),
        settings.SCREENCAPTURES_TEMP_DIRECTORY_NAME
    )
    if not os.path.isdir(dir_):
        if verbose:  # pragma: no cover
            print "Screencaps temp directory does not exist"
        return

    # every dir in this directory is expected to the event ID as a string
    for sub_dir in os.listdir(dir_):
        sub_dir_path = os.path.join(dir_, sub_dir)
        try:
            id, slug = sub_dir.split('_', 1)
        except ValueError:
            if verbose:  # pragma: no cover
                print "Unrecognized directory name", sub_dir
            continue
        try:
            event = Event.objects.get(slug=slug, id=id)
            files = _get_files(sub_dir_path)
            created = _import_files(event, files)
            if verbose:  # pragma: no cover
                print "Created", created, "pictures for", event.slug,
                print "(id=%d)" % (event.id,)
            shutil.rmtree(sub_dir_path)
        except Event.DoesNotExist:
            if verbose:  # pragma: no cover
                print "Unrecognized event", sub_dir
