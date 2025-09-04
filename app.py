import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime, timezone
from io import BytesIO
import requests


# ---------------- Green Button XML Parser ----------------
def parse_alectra_xml(uploaded_file):
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "espi": "http://naesb.org/espi"
    }
    uploaded_file.seek(0)
    tree = ET.parse(uploaded_file)
    root = tree.getroot()

    records = []
    for block in root.findall(".//atom:entry/atom:content/espi:IntervalBlock", ns):
        for reading in block.findall("espi:IntervalReading", ns):
            start = reading.find("espi:timePeriod/espi:start", ns).text
            duration = reading.find("espi:timePeriod/espi:duration", ns).text
            value = reading.find("espi:value", ns).text

            # Convert epoch → datetime UTC
            ts_utc = pd.to_datetime(int(start), unit="s", utc=True)

            records.append({
                "time": ts_utc,
                "duration_sec": int(duration),
                "load_Wh": float(value)
            })

    df = pd.DataFrame(records)
    df["load_kWh"] = df["load_Wh"] / 1000.0

    # Convert to Toronto local time
    df["time"] = df["time"].dt.tz_convert("America/Toronto")

    return df


# ---------------- PVWatts API ----------------
def hourly_solar_data_multi_year(lat, lon, start_year, end_year,
                                 system_capacity_kw=1, tilt=35, azimuth=180,
                                 losses=14, api_key="YOUR_API_KEY"):
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

    # ✅ 加入 tz 参数，避免夏令时报错
    base_df = pd.DataFrame({
        "hour": pd.date_range("2001-01-01", periods=8760, freq="H", tz="America/Toronto"),
        "poa_Wm2": poa,
        "ac_kWh": ac
    })

    all_years = []
    for year in range(start_year, end_year + 1):
        df_year = base_df.copy()
        # 用 replace(year=year) 保留原来的时区信息
        df_year["time"] = df_year["hour"].apply(lambda d: d.replace(year=year))
        df_year["year"] = year
        df_year["system_capacity_kw"] = system_capacity_kw
        all_years.append(df_year[["time", "year", "poa_Wm2", "ac_kWh", "system_capacity_kw"]])

    return pd.concat(all_years, ignore_index=True)


# ---------------- Streamlit UI ----------------
st.title("⚡ Load vs PV Generation (Green Button + PVWatts)")

uploaded_file = st.file_uploader("Upload Alectra Green Button XML file", type=["xml"])

with st.sidebar:
    st.header("PV System Parameters")
    lat = st.number_input("Latitude:", value=43.653, format="%.6f")
    lon = st.number_input("Longitude:", value=-79.383, format="%.6f")
    year_range = st.text_input("Enter year range (e.g. 2023-2024):", "2023-2023")
    system_capacity_kw = st.number_input("System capacity (kW):", min_value=1.0, value=100.0, step=10.0)
    tilt = st.slider("Panel tilt (°)", 0, 90, 35)
    azimuth = st.slider("Panel azimuth (°)", 0, 360, 180)

API_KEY = "NiW6JjfVhrZdFMiNwsQfNVuEveL67iy2Jmq9Gopz"

if uploaded_file and st.button("Run Analysis"):
    try:
        # Parse Green Button load
        load_df = parse_alectra_xml(uploaded_file)

        # Parse year range
        if "-" in year_range:
            start_year, end_year = map(int, year_range.split("-"))
        else:
            start_year = end_year = int(year_range)

        # PV Data
        pv_df = hourly_solar_data_multi_year(lat, lon, start_year, end_year,
                                             system_capacity_kw=system_capacity_kw,
                                             tilt=tilt, azimuth=azimuth,
                                             api_key=API_KEY)

        # Resample PV data to 5 min
        pv_df = pv_df.set_index("time").resample("5T").ffill().reset_index()

        # Merge
        merged = pd.merge(load_df, pv_df, on="time", how="inner")

        st.success("✅ Data merged successfully!")

        st.subheader("📊 Preview")
        st.dataframe(merged.head(50))

        # Plot load vs PV
        st.subheader("📈 Load vs PV Generation")
        st.line_chart(merged.set_index("time")[["load_kWh", "ac_kWh"]])

        # Annual totals
        st.subheader("📊 Annual Totals")
        annual = merged.groupby("year")[["load_kWh", "ac_kWh"]].sum().reset_index()
        st.dataframe(annual)

        # Download merged data
        output = BytesIO()
        merged.to_csv(output, index=False)
        st.download_button(
            label="⬇️ Download merged CSV",
            data=output.getvalue(),
            file_name="load_vs_pv.csv",
            mime="text/csv"
        )

    except Exception as e:
        st.error(f"Error: {e}")
