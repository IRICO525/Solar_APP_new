import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
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
            start = int(reading.find("espi:timePeriod/espi:start", ns).text)
            duration = int(reading.find("espi:timePeriod/espi:duration", ns).text)
            value = float(reading.find("espi:value", ns).text)

            records.append({
                "epoch": start,
                "duration_sec": duration,
                "load_Wh": value
            })

    df = pd.DataFrame(records)
    df["load_kWh"] = df["load_Wh"] / 1000.0
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

    base_df = pd.DataFrame({
        "time": pd.date_range("2001-01-01", periods=8760, freq="H", tz="America/Toronto"),
        "poa_Wm2": poa,
        "ac_kWh": ac
    })

    all_years = []
    for year in range(start_year, end_year + 1):
        df_year = base_df.copy()
        df_year["time"] = df_year["time"].apply(lambda d: d.replace(year=year))
        # 转 epoch
        df_year["epoch"] = df_year["time"].astype("int64") // 10**9
        df_year["year"] = year
        df_year["system_capacity_kw"] = system_capacity_kw
        all_years.append(df_year[["epoch", "year", "poa_Wm2", "ac_kWh", "system_capacity_kw"]])

    return pd.concat(all_years, ignore_index=True)


# ---------------- Streamlit UI ----------------
st.title("⚡ Load + PV Data Merger (Epoch Alignment)")

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

if uploaded_file and st.button("Generate File"):
    try:
        # Parse load
        load_df = parse_alectra_xml(uploaded_file)

        # Parse year range
        if "-" in year_range:
            start_year, end_year = map(int, year_range.split("-"))
        else:
            start_year = end_year = int(year_range)

        # Get PV
        pv_df = hourly_solar_data_multi_year(lat, lon, start_year, end_year,
                                             system_capacity_kw=system_capacity_kw,
                                             tilt=tilt, azimuth=azimuth,
                                             api_key=API_KEY)

        # Merge by epoch
        merged = pd.merge(load_df, pv_df, on="epoch", how="inner")

        # Export file
        output = BytesIO()
        merged.to_csv(output, index=False)
        st.download_button(
            label="⬇️ Download merged CSV",
            data=output.getvalue(),
            file_name="load_vs_pv.csv",
            mime="text/csv"
        )

        st.success("✅ File generated successfully!")

    except Exception as e:
        st.error(f"Error: {e}")
