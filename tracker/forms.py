from django import forms

from django.contrib.auth.forms import UserCreationForm
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.auth.models import User
from .models import *

class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=False)

    base_currency = forms.ChoiceField(
        choices=CURRENCY_CHOICES,
        initial="CHF",
        label="Base Currency",
        help_text="This is the currency your total wealth will be reported in."
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password1",
            "password2",
            "base_currency",
        ]

from django import forms
from .models import Position


class PositionForm(forms.ModelForm):
    auto_acquisition_value = forms.BooleanField(
        required=False,
        label="Look up price automatically",
        help_text="Use the market price on the acquisition date × quantity.",
    )

    class Meta:
        model = Position
        fields = [
            "asset_type",
            "currency",
            "description",
            "ticker",
            "quantity",
            "manual_current_value",
            "acquisition_date",
            "acquisition_value",
        ]

        widgets = {
            "acquisition_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["ticker"].required = False
        self.fields["quantity"].required = False
        self.fields["manual_current_value"].required = False
        self.fields["description"].required = False
        self.fields["acquisition_date"].required = False
        self.fields["acquisition_value"].required = False   # now optional

        for name, field in self.fields.items():
            if name != "auto_acquisition_value":            # don't style the checkbox
                field.widget.attrs.update({"class": "form-input"})

    def clean(self):
        cleaned = super().clean()

        if not cleaned.get("auto_acquisition_value"):
            return cleaned                                   # manual entry — nothing to do

        ticker = (cleaned.get("ticker") or "").strip().upper()
        date = cleaned.get("acquisition_date")
        qty = cleaned.get("quantity")

        if not ticker:
            raise forms.ValidationError(
                {"ticker": "A ticker is required for automatic price lookup."}
            )
        if not date:
            raise forms.ValidationError(
                {"acquisition_date": "A date is required for automatic price lookup."}
            )
        if not qty:
            raise forms.ValidationError(
                {"quantity": "A quantity is required for automatic price lookup."}
            )

        from marketdata.models import AssetPrice
        from marketdata.mdfiller import populateHistoricAssetPriceTable

        # brand-new ticker? backfill it so we have prices to read
        if not AssetPrice.objects.filter(ticker=ticker).exists():
            populateHistoricAssetPriceTable({ticker})

        # most recent price ON OR BEFORE the acquisition date
        # (handles buying on a weekend or market holiday)
        row = (
            AssetPrice.objects
            .filter(ticker=ticker, date__lte=date)
            .order_by("-date")
            .first()
        )

        if row is None or row.price.is_nan():
            earliest = (
                AssetPrice.objects.filter(ticker=ticker)
                .order_by("date")
                .values_list("date", flat=True)
                .first()
            )
            msg = f"No price data for {ticker} on or before {date}."
            if earliest:
                msg += f" Earliest available is {earliest}."
            msg += " Enter the acquisition value manually instead."
            raise forms.ValidationError({"acquisition_date": msg})

        # total paid = price on that date × quantity
        value = (row.price * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cleaned["acquisition_value"] = value
        self.instance.acquisition_value = value
        return cleaned

    def clean_acquisition_value(self):
        """Blank submission -> 0, not None (model's NOT NULL would fail)."""
        return self.cleaned_data.get("acquisition_value") or Decimal("0")


