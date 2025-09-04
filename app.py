import streamlit as st
import requests
import pandas as pd
from datetime import datetime
from io import BytesIO


def hourly_solar_data_multi_year(lat, lon, start_year, end_year,
                                 system_capacity_kw=1, tilt=35, azimuth=180,
                                 losses=14, api_key="YOUR_API_KEY"):
    """Get hourly solar irradiance for multiple years (using TMY repeated)."""
    url = "https://developer.nrel.gov/api/pvwatts/v6.json"
    params = {
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": system_capacity_kw,
        "azimuth": azimuth,
        "tilt": tilt,
        "array_type": 1,
        "module_type": 1,
        "losses": losses,
        "timeframe": "hourly"
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()["outputs"]

    poa = data["poa"]  # W/m²
    ac = data["ac"]    # kWh

    # Base year (dummy TMY year)
    base_df = pd.DataFrame({
        "hour": pd.date_range("2001-01-01", periods=8760, freq="H"),
        "poa_Wm2": poa,
        "poa_kWhm2": [x / 1000.0 for x in poa],
        "ac_kWh": ac
    })

    all_years = []
    for year in range(start_year, end_year + 1):
        df_year = base_df.copy()
        df_year["time"] = df_year["hour"].apply(lambda d: d.replace(year=year))
        df_year["epoch"] = df_year["time"].apply(lambda d: int(datetime.timestamp(d)))
        df_year["year"] = year
        df_year["system_capacity_kw"] = system_capacity_kw
        all_years.append(df_year[["epoch", "time", "year", "poa_Wm2",
                                  "poa_kWhm2", "ac_kWh", "system_capacity_kw"]])

    final_df = pd.concat(all_years, ignore_index=True)
    return final_df


# ----------------- Streamlit UI -----------------

st.title("☀️ Solar Irradiance & PV Production")

with st.sidebar:
    st.header("⚙️ Input parameters")
    lat = st.number_input("Latitude:", value=43.653, format="%.6f")
    lon = st.number_input("Longitude:", value=-79.383, format="%.6f")
    year_range = st.text_input("Enter year range (e.g. 2023-2025):", "2023-2025")
    capacities_input = st.text_input("System capacities (kW, comma separated):", "100,200")
    tilt = st.slider("Panel tilt (°)", 0, 90, 35)
    azimuth = st.slider("Panel azimuth (°)", 0, 360, 180)

API_KEY = "NiW6JjfVhrZdFMiNwsQfNVuEveL67iy2Jmq9Gopz"

if st.button("Generate Data"):
    try:
        if "-" in year_range:
            start_year, end_year = map(int, year_range.split("-"))
        else:
            start_year = end_year = int(year_range)

        # Parse system capacities
        capacities = [float(c.strip()) for c in capacities_input.split(",") if c.strip()]

        dfs = []
        for cap in capacities:
            df_cap = hourly_solar_data_multi_year(lat, lon, start_year, end_year,
                                                  system_capacity_kw=cap,
                                                  tilt=tilt, azimuth=azimuth,
                                                  api_key=API_KEY)
            dfs.append(df_cap)

        df_all = pd.concat(dfs, ignore_index=True)

        st.success(f"✅ Data generated for {lat:.4f}, {lon:.4f} ({start_year}-{end_year}), "
                   f"tilt {tilt}°, azimuth {azimuth}° with capacities {capacities} kW.")

        st.subheader("📊 Preview (first 50 rows)")
        st.dataframe(df_all.head(50))

        # Annual totals
        st.subheader("📈 Annual totals (kWh)")
        annual_totals = df_all.groupby(["year", "system_capacity_kw"])["ac_kWh"].sum().reset_index()
        st.line_chart(annual_totals.pivot(index="year", columns="system_capacity_kw", values="ac_kWh"))
        st.table(annual_totals)

        # Monthly profile
        st.subheader("📆 Monthly average profile")
        df_all["month"] = df_all["time"].dt.month
        monthly = df_all.groupby(["month", "system_capacity_kw"])["ac_kWh"].mean().reset_index()
        st.line_chart(monthly.pivot(index="month", columns="system_capacity_kw", values="ac_kWh"))

        # Typical day (summer solstice)
        st.subheader("🌞 Typical day profile (June 21)")
        typical_day = df_all[df_all["time"].dt.strftime("%m-%d") == "06-21"].copy()
        typical_day["hour_of_day"] = typical_day["time"].dt.hour
        day_avg = typical_day.groupby(["hour_of_day", "system_capacity_kw"])["ac_kWh"].mean().reset_index()
        st.line_chart(day_avg.pivot(index="hour_of_day", columns="system_capacity_kw", values="ac_kWh"))

        # Download
        output = BytesIO()
        df_all.to_csv(output, index=False)
        st.download_button(
            label="⬇️ Download full CSV",
            data=output.getvalue(),
            file_name=f"solar_hourly_{start_year}_{end_year}.csv",
            mime="text/csv"
        )

    except Exception as e:
        st.error(f"Error: {e}")
