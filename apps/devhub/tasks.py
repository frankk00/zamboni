import json
import logging
import os
import sys
import traceback

from django.conf import settings
from django.core.management import call_command
from celeryutils import task

from addons.models import Addon
import amo
from amo.decorators import write, set_modified_on
from amo.utils import resize_image
from files.models import FileUpload, File, FileValidation
from applications.management.commands import dump_apps

from PIL import Image

log = logging.getLogger('z.devhub.task')


@task(queue='devhub')
@write
def validator(upload_id, **kw):
    if not settings.VALIDATE_ADDONS:
        return None
    log.info('VALIDATING: %s' % upload_id)
    upload = FileUpload.objects.get(pk=upload_id)
    try:
        result = run_validator(upload.path)
        upload.validation = result
        upload.save()  # We want to hit the custom save().
    except:
        # Store the error with the FileUpload job, then raise
        # it for normal logging.
        tb = traceback.format_exception(*sys.exc_info())
        upload.update(task_error=''.join(tb))
        raise


@task(queue='devhub')
@write
def file_validator(file_id, **kw):
    if not settings.VALIDATE_ADDONS:
        return None
    log.info('VALIDATING file: %s' % file_id)
    file = File.objects.get(pk=file_id)
    # Unlike upload validation, let the validator
    # raise an exception if there is one.
    result = run_validator(file.file_path)
    return FileValidation.from_json(file, result)


def run_validator(file_path, for_appversions=None, test_all_tiers=False,
                  overrides=None):
    """A pre-configured wrapper around the addon validator.

    *file_path*
        Path to addon / extension file to validate.

    *for_appversions=None*
        An optional dict of application versions to validate this addon
        for. The key is an application GUID and its value is a list of
        versions.

    *test_all_tiers=False*
        When False (default) the validator will not continue if it
        encounters fatal errors.  When True, all tests in all tiers are run.
        See bug 615426 for discussion on this default.

    *overrides=None*
        Normally the validator gets info from install.rdf but there are a
        few things we need to override. See validator for supported overrides.
        Example: {'targetapp_maxVersion': {'<app guid>': '<version>'}}

    To validate the addon for compatibility with Firefox 5 and 6,
    you'd pass in::

        for_appversions={amo.FIREFOX.guid: ['5.0.*', '6.0.*']}

    Not all application versions will have a set of registered
    compatibility tests.
    """

    from validator.validate import validate

    # TODO(Kumar) remove this when validator is fixed, see bug 620503
    from validator.testcases import scripting
    scripting.SPIDERMONKEY_INSTALLATION = settings.SPIDERMONKEY
    import validator.constants
    validator.constants.SPIDERMONKEY_INSTALLATION = settings.SPIDERMONKEY

    apps = dump_apps.Command.JSON_PATH
    if not os.path.exists(apps):
        call_command('dump_apps')

    return validate(file_path,
                    for_appversions=for_appversions,
                    format='json',
                    # When False, this flag says to stop testing after one
                    # tier fails.
                    determined=test_all_tiers,
                    approved_applications=apps,
                    spidermonkey=settings.SPIDERMONKEY,
                    overrides=overrides)


@task(rate_limit='4/m')
@write
def flag_binary(ids, **kw):
    log.info('[%s@%s] Flagging binary addons starting with id: %s...'
             % (len(ids), flag_binary.rate_limit, ids[0]))
    addons = Addon.objects.filter(pk__in=ids).no_transforms()

    for addon in addons:
        try:
            log.info('Validating addon with id: %s' % addon.pk)
            file = File.objects.filter(version__addon=addon).latest('created')
            result = run_validator(file.file_path)
            binary = (json.loads(result)['metadata']
                          .get('contains_binary_extension', False))
            log.info('Setting binary for addon with id: %s to %s'
                     % (addon.pk, binary))
            addon.update(binary=binary)
        except Exception, err:
            log.error('Failed to run validation on addon id: %s, %s'
                      % (addon.pk, err))


@task
@set_modified_on
def resize_icon(src, dst, size, **kw):
    """Resizes addon icons."""
    log.info('[1@None] Resizing icon: %s' % dst)
    try:
        if isinstance(size, list):
            for s in size:
                resize_image(src, '%s-%s.png' % (dst, s), (s, s),
                             remove_src=False)
            os.remove(src)
        else:
            resize_image(src, dst, (size, size), remove_src=True)
        return True
    except Exception, e:
        log.error("Error saving addon icon: %s" % e)


@task
@set_modified_on
def resize_preview(src, thumb_dst, full_dst, **kw):
    """Resizes preview images."""
    log.info('[1@None] Resizing preview: %s' % thumb_dst)
    try:
        # Generate the thumb.
        size = amo.ADDON_PREVIEW_SIZES[0]
        resize_image(src, thumb_dst, size, remove_src=False)

        # Resize the original.
        size = amo.ADDON_PREVIEW_SIZES[1]
        resize_image(src, full_dst, size, remove_src=True)
        return True
    except Exception, e:
        log.error("Error saving preview: %s" % e)

@task
@set_modified_on
def resize_preview_store_size(src, instance, **kw):
    """Resizes preview images and stores the sizes on the preview."""
    thumb_dst, full_dst = instance.thumbnail_path, instance.image_path
    sizes = {}
    log.info('[1@None] Resizing preview and storing size: %s' % thumb_dst)
    try:
        sizes['thumbnail'] = resize_image(src, thumb_dst,
                                          amo.ADDON_PREVIEW_SIZES[0],
                                          remove_src=False)
        sizes['image'] = resize_image(src, full_dst,
                                      amo.ADDON_PREVIEW_SIZES[1],
                                      remove_src=False)
        instance.sizes = sizes
        instance.save()
        return True
    except Exception, e:
        log.error("Error saving preview: %s" % e)

@task
@write
def get_preview_sizes(ids, **kw):
    log.info('[%s@%s] Getting preview sizes for addons starting at id: %s...'
             % (len(ids), get_preview_sizes.rate_limit, ids[0]))
    addons = Addon.objects.filter(pk__in=ids).no_transforms()

    for addon in addons:
        previews = addon.previews.all()
        log.info('Found %s previews for: %s' % (previews.count(), addon.pk))
        for preview in previews:
            try:
                log.info('Getting size for preview: %s' % preview.pk)
                sizes = {
                    'thumbnail':  Image.open(preview.thumbnail_path).size,
                    'image':  Image.open(preview.image_path).size,
                }
                preview.update(sizes=sizes)
            except Exception, err:
                log.error('Failed to find size of preview: %s, error: %s'
                          % (addon.pk, err))


@task
@write
def convert_purified(ids, **kw):
    log.info('[%s@%s] Converting fields to purified starting at id: %s...'
             % (len(ids), convert_purified.rate_limit, ids[0]))
    fields = ['the_reason', 'the_future']
    for addon in Addon.objects.filter(pk__in=ids):
        flag = False
        for field in fields:
            value = getattr(addon, field)
            if value:
                value.clean()
                if (value.localized_string_clean != value.localized_string):
                    flag = True
        if flag:
            log.info('Saving addon: %s to purify fields' % addon.pk)
            addon.save()
