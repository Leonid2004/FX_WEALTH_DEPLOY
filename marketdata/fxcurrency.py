import requests
import pandas as pd
import io


DATA_START = "2000-01-01"


def getHistoricalRates(start=DATA_START):
    request_url = (
        "https://data-api.ecb.europa.eu/service/data/"
        "EXR/D.CHF+USD+GBP.EUR.SP00.A"
        f"?startPeriod={start}"
    )

    response = requests.get(
        request_url,
        headers={"Accept": "text/csv"},
        timeout=120,
    )
    response.raise_for_status()

    df = pd.read_csv(io.StringIO(response.text))

    ts = df[["TIME_PERIOD", "CURRENCY", "CURRENCY_DENOM", "OBS_VALUE"]].copy()
    ts["TIME_PERIOD"] = pd.to_datetime(ts["TIME_PERIOD"])
    ts["OBS_VALUE"] = pd.to_numeric(ts["OBS_VALUE"], errors="coerce")
    ts = ts.dropna(subset=["OBS_VALUE"])

    today = pd.Timestamp.today().normalize()
    ts = ts[ts["TIME_PERIOD"] <= today]
    ts = ts.sort_values("TIME_PERIOD")

    history = {}
    for _, row in ts.iterrows():
        d = row["TIME_PERIOD"].date()
        pair = f"{row['CURRENCY_DENOM']}/{row['CURRENCY']}"

        if d not in history:
            history[d] = {"EUR/EUR": [1.0, "ECB"]}

        history[d][pair] = [row["OBS_VALUE"], "ECB"]

    return history


def getFullHistoricalFxRates(start=DATA_START):
    history = getHistoricalRates(start)
    src = "computed"

    for d, r in history.items():
        if not all(k in r for k in ("EUR/CHF", "EUR/GBP", "EUR/USD")):
            continue

        r["CHF/EUR"] = [1 / r["EUR/CHF"][0], src]
        r["GBP/EUR"] = [1 / r["EUR/GBP"][0], src]
        r["USD/EUR"] = [1 / r["EUR/USD"][0], src]

        r["CHF/GBP"] = [r["EUR/GBP"][0] / r["EUR/CHF"][0], src]
        r["GBP/CHF"] = [1 / r["CHF/GBP"][0], src]

        r["CHF/USD"] = [r["EUR/USD"][0] / r["EUR/CHF"][0], src]
        r["USD/CHF"] = [1 / r["CHF/USD"][0], src]

        r["USD/GBP"] = [r["EUR/GBP"][0] / r["EUR/USD"][0], src]
        r["GBP/USD"] = [1 / r["USD/GBP"][0], src]

    return history


def getInitialRates():
    request_url = (
        "https://data-api.ecb.europa.eu/service/data/"
        "EXR/D.CHF+USD+GBP.EUR.SP00.A"
    )

    response = requests.get(
        request_url,
        headers={"Accept": "text/csv"},
        timeout=30,
    )

    response.raise_for_status()

    print(response)
    print(response.url)

    df = pd.read_csv(io.StringIO(response.text))

    # Keep only the columns we need
    ts = df[
        [
            "TIME_PERIOD",
            "CURRENCY",
            "CURRENCY_DENOM",
            "OBS_VALUE",
        ]
    ].copy()

    ts["TIME_PERIOD"] = pd.to_datetime(ts["TIME_PERIOD"])

    ts = ts.dropna(subset=["OBS_VALUE"])

    today = pd.Timestamp.today().normalize()

    ts = ts[ts["TIME_PERIOD"] <= today]

    ts = ts.sort_values("TIME_PERIOD")

    # use the most recent date on which ALL three currencies were published,
    # so every stored rate (and every derived cross) is from one single day
    per_date = ts.groupby("TIME_PERIOD")["CURRENCY"].nunique()
    complete_dates = per_date[per_date == 3].index
    ref_date = complete_dates.max()

    latest_rates = ts[ts["TIME_PERIOD"] == ref_date].sort_values("CURRENCY")

    print("\nLatest available ECB rates:")
    print(
        latest_rates[
            [
                "TIME_PERIOD",
                "CURRENCY",
                "CURRENCY_DENOM",
                "OBS_VALUE",
            ]
        ].to_string(index=False)
    )

    rates = {
        f"{row['CURRENCY_DENOM']}/{row['CURRENCY']}": [row["OBS_VALUE"], "ECB"]
        for _, row in latest_rates.iterrows()
    }

    rates["EUR/EUR"] = [1.0, "ECB"]

    print(rates)
    return rates, ref_date.date()


# returns: {'CHF/EUR': 0.9224, 'GBP/EUR': 0.86471, 'USD/EUR': 1.1594, 'EUR/EUR': 1.0}
# rates['CHF/EUR']
# 0.9224

def getFullFxRates():
    r = getInitialRates()
    src = 'computed'

    r[0]['CHF/EUR'] = [1 / r[0]['EUR/CHF'][0], src]
    r[0]['GBP/EUR'] = [1 / r[0]['EUR/GBP'][0], src]
    r[0]['USD/EUR'] = [1 / r[0]['EUR/USD'][0], src]

    r[0]['CHF/GBP'] = [r[0]['EUR/GBP'][0] / r[0]['EUR/CHF'][0], src]
    r[0]['GBP/CHF'] = [1 / r[0]['CHF/GBP'][0], src]

    r[0]['CHF/USD'] = [r[0]['EUR/USD'][0] / r[0]['EUR/CHF'][0], src]
    r[0]['USD/CHF'] = [1 / r[0]['CHF/USD'][0], src]

    r[0]['USD/GBP'] = [r[0]['EUR/GBP'][0] / r[0]['EUR/USD'][0], src]
    r[0]['GBP/USD'] = [1 / r[0]['USD/GBP'][0], src]

    return r[0], r[1]