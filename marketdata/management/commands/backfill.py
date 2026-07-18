from django.core.management.base import BaseCommand


from marketdata.mdfiller import (
    populateHistoricFxTable,
    backfillAllPortfolioTickers,
    fillTheFxRateTable,
)

class Command(BaseCommand):
    help = "Backfill full FX-rate history (ECB) and price history for every portfolio ticker."

    def handle(self, *args, **options):
        self.stdout.write("Backfilling FX rates from ECB (2000 -> today)...")
        n_fx = populateHistoricFxTable()
        self.stdout.write(self.style.SUCCESS(f"  {n_fx} FX rows processed"))

        self.stdout.write("Backfilling asset prices for portfolio tickers...")
        n_px = backfillAllPortfolioTickers()
        self.stdout.write(self.style.SUCCESS(f"  {n_px} price rows processed"))

        self.stdout.write("Refreshing today's FX rates...")
        fillTheFxRateTable()
        self.stdout.write(self.style.SUCCESS("  done"))

#usage: python manage.py backfill