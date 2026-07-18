from django.db import models

# Create your models here.

class FxRate(models.Model):
    base = models.CharField(max_length=3)          # 'EUR'
    quote = models.CharField(max_length=3)         # 'CHF'
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    date = models.DateField()                       # the ECB TIME_PERIOD
    source = models.CharField(max_length=20, default='ECB')
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['base', 'quote', 'date'],
                name='unique_rate_per_pair_date',
            )
        ]
        indexes = [
            models.Index(fields=['base', 'quote', 'date']),
        ]
        ordering = ['-date', 'base', 'quote']

    def __str__(self):
        return f"{self.base}/{self.quote} {self.rate} @ {self.date}"


class AssetPrice(models.Model):
    ticker = models.CharField(max_length=20)
    price = models.DecimalField(max_digits=18, decimal_places=6)
    currency = models.CharField(max_length=3)      # the price's native currency
    date = models.DateField()
    source = models.CharField(max_length=20, default="yfinance")
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "date", "source"],
                name="unique_price_per_ticker_date",
            )
        ]
        indexes = [models.Index(fields=["ticker", "date"])]
        ordering = ["-date", "ticker"]