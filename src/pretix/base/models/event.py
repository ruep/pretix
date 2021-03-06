import string
import uuid
from datetime import date, datetime, time

import pytz
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.mail import get_connection
from django.core.validators import RegexValidator
from django.db import models
from django.template.defaultfilters import date as _date
from django.utils.crypto import get_random_string
from django.utils.timezone import make_aware, now
from django.utils.translation import ugettext_lazy as _
from i18nfield.fields import I18nCharField, I18nTextField

from pretix.base.email import CustomSMTPBackend
from pretix.base.models.base import LoggedModel
from pretix.base.validators import EventSlugBlacklistValidator
from pretix.helpers.daterange import daterange

from ..settings import settings_hierarkey
from .organizer import Organizer


@settings_hierarkey.add(parent_field='organizer', cache_namespace='event')
class Event(LoggedModel):
    """
    This model represents an event. An event is anything you can buy
    tickets for.

    :param organizer: The organizer this event belongs to
    :type organizer: Organizer
    :param name: This event's full title
    :type name: str
    :param slug: A short, alphanumeric, all-lowercase name for use in URLs. The slug has to
                 be unique among the events of the same organizer.
    :type slug: str
    :param live: Whether or not the shop is publicly accessible
    :type live: bool
    :param currency: The currency of all prices and payments of this event
    :type currency: str
    :param date_from: The datetime this event starts
    :type date_from: datetime
    :param date_to: The datetime this event ends
    :type date_to: datetime
    :param presale_start: No tickets will be sold before this date.
    :type presale_start: datetime
    :param presale_end: No tickets will be sold after this date.
    :type presale_end: datetime
    :param location: venue
    :type location: str
    :param plugins: A comma-separated list of plugin names that are active for this
                    event.
    :type plugins: str
    """

    settings_namespace = 'event'
    CURRENCY_CHOICES = [(c.alpha_3, c.alpha_3 + " - " + c.name) for c in settings.CURRENCIES]
    organizer = models.ForeignKey(Organizer, related_name="events", on_delete=models.PROTECT)
    name = I18nCharField(
        max_length=200,
        verbose_name=_("Name"),
    )
    slug = models.SlugField(
        max_length=50, db_index=True,
        help_text=_(
            "Should be short, only contain lowercase letters and numbers, and must be unique among your events. "
            "We recommend some kind of abbreviation or a date with less than 10 characters that can be easily "
            "remembered, but you can also choose to use a random value. "
            "This will be used in URLs, order codes, invoice numbers, and bank transfer references."),
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9.-]+$",
                message=_("The slug may only contain letters, numbers, dots and dashes."),
            ),
            EventSlugBlacklistValidator()
        ],
        verbose_name=_("Short form"),
    )
    live = models.BooleanField(default=False, verbose_name=_("Shop is live"))
    currency = models.CharField(max_length=10,
                                verbose_name=_("Default currency"),
                                choices=CURRENCY_CHOICES,
                                default=settings.DEFAULT_CURRENCY)
    date_from = models.DateTimeField(verbose_name=_("Event start time"))
    date_to = models.DateTimeField(null=True, blank=True,
                                   verbose_name=_("Event end time"))
    date_admission = models.DateTimeField(null=True, blank=True,
                                          verbose_name=_("Admission time"))
    is_public = models.BooleanField(default=False,
                                    verbose_name=_("Visible in public lists"),
                                    help_text=_("If selected, this event may show up on the ticket system's start page "
                                                "or an organization profile."))
    presale_end = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("End of presale"),
        help_text=_("No products will be sold after this date."),
    )
    presale_start = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Start of presale"),
        help_text=_("No products will be sold before this date."),
    )
    location = I18nTextField(
        null=True, blank=True,
        max_length=200,
        verbose_name=_("Location"),
    )
    plugins = models.TextField(
        null=True, blank=True,
        verbose_name=_("Plugins"),
    )

    class Meta:
        verbose_name = _("Event")
        verbose_name_plural = _("Events")
        ordering = ("date_from", "name")

    def __str__(self):
        return str(self.name)

    def save(self, *args, **kwargs):
        obj = super().save(*args, **kwargs)
        self.get_cache().clear()
        return obj

    def clean(self):
        if self.presale_start and self.presale_end and self.presale_start > self.presale_end:
            raise ValidationError({'presale_end': _('The end of the presale period has to be later than its start.')})
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValidationError({'date_to': _('The end of the event has to be later than its start.')})
        super().clean()

    def get_plugins(self) -> "list[str]":
        """
        Returns the names of the plugins activated for this event as a list.
        """
        if self.plugins is None:
            return []
        return self.plugins.split(",")

    def get_date_from_display(self, tz=None, show_times=True) -> str:
        """
        Returns a formatted string containing the start date of the event with respect
        to the current locale and to the ``show_times`` setting.
        """
        tz = tz or pytz.timezone(self.settings.timezone)
        return _date(
            self.date_from.astimezone(tz),
            "DATETIME_FORMAT" if self.settings.show_times and show_times else "DATE_FORMAT"
        )

    def get_time_from_display(self, tz=None) -> str:
        """
        Returns a formatted string containing the start time of the event, ignoring
        the ``show_times`` setting.
        """
        tz = tz or pytz.timezone(self.settings.timezone)
        return _date(
            self.date_from.astimezone(tz), "TIME_FORMAT"
        )

    def get_date_to_display(self, tz=None) -> str:
        """
        Returns a formatted string containing the start date of the event with respect
        to the current locale and to the ``show_times`` setting. Returns an empty string
        if ``show_date_to`` is ``False``.
        """
        tz = tz or pytz.timezone(self.settings.timezone)
        if not self.settings.show_date_to or not self.date_to:
            return ""
        return _date(
            self.date_to.astimezone(tz),
            "DATETIME_FORMAT" if self.settings.show_times else "DATE_FORMAT"
        )

    def get_date_range_display(self, tz=None) -> str:
        tz = tz or pytz.timezone(self.settings.timezone)
        if not self.settings.show_date_to or not self.date_to:
            return _date(self.date_from.astimezone(tz), "DATE_FORMAT")
        return daterange(self.date_from.astimezone(tz), self.date_to.astimezone(tz))

    def get_cache(self) -> "pretix.base.cache.ObjectRelatedCache":
        """
        Returns an :py:class:`ObjectRelatedCache` object. This behaves equivalent to
        Django's built-in cache backends, but puts you into an isolated environment for
        this event, so you don't have to prefix your cache keys. In addition, the cache
        is being cleared every time the event or one of its related objects change.
        """
        from pretix.base.cache import ObjectRelatedCache

        return ObjectRelatedCache(self)

    @property
    def presale_has_ended(self):
        if self.presale_end and now() > self.presale_end:
            return True
        return False

    @property
    def presale_is_running(self):
        if self.presale_start and now() < self.presale_start:
            return False
        if self.presale_end and now() > self.presale_end:
            return False
        return True

    def lock(self):
        """
        Returns a contextmanager that can be used to lock an event for bookings.
        """
        from pretix.base.services import locking

        return locking.LockManager(self)

    def get_mail_backend(self, force_custom=False):
        if self.settings.smtp_use_custom or force_custom:
            return CustomSMTPBackend(host=self.settings.smtp_host,
                                     port=self.settings.smtp_port,
                                     username=self.settings.smtp_username,
                                     password=self.settings.smtp_password,
                                     use_tls=self.settings.smtp_use_tls,
                                     use_ssl=self.settings.smtp_use_ssl,
                                     fail_silently=False)
        else:
            return get_connection(fail_silently=False)

    @property
    def payment_term_last(self):
        tz = pytz.timezone(self.settings.timezone)
        return make_aware(datetime.combine(
            self.settings.get('payment_term_last', as_type=date),
            time(hour=23, minute=59, second=59)
        ), tz)

    def copy_data_from(self, other):
        from . import ItemAddOn, ItemCategory, Item, Question, Quota
        from ..signals import event_copy_data

        self.plugins = other.plugins
        self.save()

        category_map = {}
        for c in ItemCategory.objects.filter(event=other):
            category_map[c.pk] = c
            c.pk = None
            c.event = self
            c.save()

        item_map = {}
        variation_map = {}
        for i in Item.objects.filter(event=other).prefetch_related('variations'):
            vars = list(i.variations.all())
            item_map[i.pk] = i
            i.pk = None
            i.event = self
            if i.picture:
                i.picture.save(i.picture.name, i.picture)
            if i.category_id:
                i.category = category_map[i.category_id]
            i.save()
            for v in vars:
                variation_map[v.pk] = v
                v.pk = None
                v.item = i
                v.save()

        for ia in ItemAddOn.objects.filter(base_item__event=other).prefetch_related('base_item', 'addon_category'):
            ia.pk = None
            ia.base_item = item_map[ia.base_item.pk]
            ia.addon_category = category_map[ia.addon_category.pk]
            ia.save()

        for q in Quota.objects.filter(event=other).prefetch_related('items', 'variations'):
            items = list(q.items.all())
            vars = list(q.variations.all())
            q.pk = None
            q.event = self
            q.save()
            for i in items:
                if i.pk in item_map:
                    q.items.add(item_map[i.pk])
            for v in vars:
                q.variations.add(variation_map[v.pk])

        for q in Question.objects.filter(event=other).prefetch_related('items', 'options'):
            items = list(q.items.all())
            opts = list(q.options.all())
            q.pk = None
            q.event = self
            q.save()
            for i in items:
                q.items.add(item_map[i.pk])
            for o in opts:
                o.pk = None
                o.question = q
                o.save()

        for s in other.settings._objects.all():
            s.object = self
            s.pk = None
            if s.value.startswith('file://'):
                fi = default_storage.open(s.value[7:], 'rb')
                nonce = get_random_string(length=8)
                fname = '%s/%s/%s.%s.%s' % (
                    self.organizer.slug, self.slug, s.key, nonce, s.value.split('.')[-1]
                )
                newname = default_storage.save(fname, fi)
                s.value = 'file://' + newname
            s.save()

        event_copy_data.send(sender=self, other=other)

    def get_payment_providers(self) -> dict:
        from ..signals import register_payment_providers

        responses = register_payment_providers.send(self)
        providers = {}
        for receiver, response in responses:
            if not isinstance(response, list):
                response = [response]
            for p in response:
                pp = p(self)
                providers[pp.identifier] = pp
        return providers


def generate_invite_token():
    return get_random_string(length=32, allowed_chars=string.ascii_lowercase + string.digits)


class EventLock(models.Model):
    event = models.CharField(max_length=36, primary_key=True)
    date = models.DateTimeField(auto_now=True)
    token = models.UUIDField(default=uuid.uuid4)


class RequiredAction(models.Model):
    """
    Represents an action that is to be done by an admin. The admin will be
    displayed a list of actions to do.

    :param datatime: The timestamp of the required action
    :type datetime: datetime
    :param user: The user that performed the action
    :type user: User
    :param done: If this action has been completed or dismissed
    :type done: bool
    :param action_type: The type of action that has to be performed. This is
       used to look up the renderer used to describe the action in a human-
       readable way. This should be some namespaced value using dotted
       notation to avoid duplicates, e.g.
       ``"pretix.plugins.banktransfer.incoming_transfer"``.
    :type action_type: str
    :param data: Arbitrary data that can be used by the log action renderer
    :type data: str
    """
    datetime = models.DateTimeField(auto_now_add=True, db_index=True)
    done = models.BooleanField(default=False)
    user = models.ForeignKey('User', null=True, blank=True, on_delete=models.PROTECT)
    event = models.ForeignKey('Event', null=True, blank=True, on_delete=models.CASCADE)
    action_type = models.CharField(max_length=255)
    data = models.TextField(default='{}')

    class Meta:
        ordering = ('datetime',)

    def display(self, request):
        from ..signals import requiredaction_display

        for receiver, response in requiredaction_display.send(self.event, action=self, request=request):
            if response:
                return response
        return self.action_type
