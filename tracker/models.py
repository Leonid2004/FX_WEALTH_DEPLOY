from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
# Create your models here.
CURRENCY_CHOICES = (
      ('USD', 'USD'),
      ('EUR', 'EUR'),
      ('CHF', 'CHF'),
      ('GBP', 'GBP')
)
class Portfolio(models.Model):
      user = models.OneToOneField(User, on_delete=models.CASCADE)
      base_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="CHF")
      created_at = models.DateTimeField(auto_now_add=True)
      last_updated_at = models.DateTimeField(auto_now=True)

ASSET_CHOICES = ( #Do not CHANGE ORDER!
      ("listed_equity", "Listed Equity"),
      ("etf","ETF"),
      ("bond","Bond"),
      ("cash","Cash"),
      ("real_estate","Real Estate"),
      ("private_holding","Private Holding"),
      ("other","Other")
)
class Position(models.Model):
      portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE)
      asset_type = models.CharField(max_length=20, choices=ASSET_CHOICES, default="listed_equity")
      ticker = models.CharField(max_length=20,blank=True, default="")
      company_name = models.CharField(max_length=120, blank=True, default="")
      currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
      quantity = models.DecimalField(default=0,max_digits=15,decimal_places=2)
      acquisition_date = models.DateField(blank=True, null=True)
      acquisition_value = models.DecimalField(max_digits=15, decimal_places=2, default=0)
      description = models.TextField(blank=True, null=True)
      manual_current_value = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)

      def clean(self):

            # Normalize ticker first

            if self.ticker:
                  self.ticker = self.ticker.strip().upper()

            ticker_based_assets = [

                  ASSET_CHOICES[0][0],

                  ASSET_CHOICES[1][0]

            ]

            manual_value_assets = [

                  ASSET_CHOICES[3][0],

                  ASSET_CHOICES[4][0],

                  ASSET_CHOICES[5][0],

            ]

            if self.asset_type in ticker_based_assets:

                  if not self.ticker:
                        raise ValidationError({

                              "ticker": "Ticker is required for listed equities and ETFs."

                        })

                  if self.quantity is None or self.quantity <= 0:
                        raise ValidationError({

                              "quantity": "Quantity must be a positive number for listed equities and ETFs."

                        })

                  if self.manual_current_value is not None:
                        raise ValidationError({

                              "manual_current_value": "Do not enter manual current value for ticker-based assets."

                        })

            if self.asset_type in manual_value_assets:

                  if self.manual_current_value is None:
                        raise ValidationError({

                              "manual_current_value": "Manual current value is required for this asset type."

                        })

                  if self.ticker:
                        raise ValidationError({

                              "ticker": "Ticker should only be used for listed equities and ETFs."

                        })

                  if self.quantity:
                        raise ValidationError({

                              "quantity": "Quantity should only be used for listed equities and ETFs."

                        })

      def save(self, *args, **kwargs):
            if self.ticker:
                  self.ticker = self.ticker.strip().upper()

            # fetch the company name once, on first save
            if self.ticker and not self.company_name:
                  from marketdata.pyfinancedata import getTickerName
                  self.company_name = getTickerName(self.ticker)

            super().save(*args, **kwargs)

      def current_local_value(self, price_lookup=None):
            """Value in the position's OWN currency, before FX conversion."""
            ticker_based = {ASSET_CHOICES[0][0], ASSET_CHOICES[1][0]}  # listed_equity, etf

            if self.asset_type in ticker_based:
                  if not price_lookup:
                        return None
                  price = price_lookup.get(self.ticker)
                  if price is None:
                        return None
                  return self.quantity * Decimal(str(price))

            # manual-value assets (cash, real_estate, private_holding) and any
            # bond/other that have a manual value set
            if self.manual_current_value is not None:
                  return self.manual_current_value

            return None