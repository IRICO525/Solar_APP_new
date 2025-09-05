import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
from io import BytesIO
import requests


# ---------------- Green Button XML Parser (5min -> hourly by Toronto local hour) ----------------
def parse_alectra_xml_hourly(uploaded_file):
    ns = {"atom": "http://www.w3.org/2005/Atom", "espi": "http://naesb.org/espi"}
    uploaded_file.seek(0)
    tree = ET.parse(uploaded_file)
    root = tree.getroot()

    records = []
    for block in root.findall(".//atom:entry/atom:content/espi:IntervalBlock", ns):
        for reading in block.findall("espi:IntervalReading", ns):
            start = int(reading.find("espi:timePeriod/espi:start", ns).text)       # epoch (s), UTC
            duration = int(reading.find("espi:timePeriod/espi:duration", ns).text) # seconds (应为300)
            value_wh = float(reading.find("espi:value", ns).text)                  # Wh

            records.append({"epoch_5min": start, "duration_sec": duration, "load_Wh": value_wh})

    df5 = pd.DataFrame(records)
    if df5.empty:
        return pd.DataFrame(columns=["epoch","多伦多时间","load kWh"])

    # 转成带时区时间
    df5["time_utc"] = pd.to_datetime(df5["epoch_5min"], unit="s", utc=True)
    # 转到多伦多，并按“多伦多本地时间”的整点做分箱
    df5["toronto_time"] = df5["time_utc"].dt.tz_convert("America/Toronto")
    df5["toronto_hour"] = df5["toronto_time"].dt.floor("H")

    # 每小时用电量（kWh）= 该小时内所有 5 分钟记录之和（Wh/1000）
    hourly = (
        df5.groupby("toronto_hour", as_index=False)["load_Wh"].sum()
        .rename(columns={"load_Wh": "load_Wh_sum"})
    )
    hourly["load kWh"] = hourly["load_Wh_sum"] / 1000.0

    # 生成该小时起点的 epoch（UTC 秒）
    hourly["epoch"] = (hourly["toronto_hour"].dt.tz_convert("UTC").astype("int64") // 10**9)

    # 整理列
    hourly = hourly.rename(columns={"toronto_hour": "多伦多时间"})
    hourly = hourly[["epoch", "多伦多时间", "load kWh"]]

    return hourly


# ---------------- PVWatts API -> hourly Toronto local -> epoch ----------------
def pvwatts_hourly_with_epoch(lat, lon, start_year, end_year,
                              system_capacity_kw=100.0, tilt=35, azimuth=180,
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
        "timeframe": "hourly",
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()["outputs"]

    ac = data["ac"]       # kWh (hourly)
    poa = data.get("poa") # 可选，不需要也行

    # 以“多伦多本地时间”的 2001 年小时序列为模板
    base = pd.DataFrame({
        "多伦多时间": pd.date_range("2001-01-01", periods=8760, freq="H", tz="America/Toronto"),
        "ac_kWh": ac
    })
    if poa:
        base["poa_Wm2"] = poa

    # 展开到目标年份，并生成 epoch（UTC 秒）
    out = []
    for year in range(start_year, end_year + 1):
        dfy = base.copy()
        dfy["多伦多时间"] = dfy["多伦多时间"].apply(lambda d: d.replace(year=year))
        dfy["epoch"] = (dfy["多伦多时间"].dt.tz_convert("UTC").astype("int64") // 10**9)
        dfy["year"] = year
        out.append(dfy[["epoch", "多伦多时间", "ac_kWh", "year"]])

    return pd.concat(out, ignore_index=True)


# ---------------- Streamlit UI ----------------
st.title("⚡ 合并导出：每小时负荷 + 光伏发电（按 epoch 对齐）")

uploaded_file = st.file_uploader("上传 Alectra Green Button XML", type=["xml"])

with st.sidebar:
    st.header("PV 参数")
    lat = st.number_input("Latitude", value=43.653, format="%.6f")
    lon = st.number_input("Longitude", value=-79.383, format="%.6f")
    year_range = st.text_input("Year range (e.g. 2023-2024)", "2023-2023")
    system_capacity_kw = st.number_input("System capacity (kW)", min_value=1.0, value=300.0, step=10.0)
    tilt = st.slider("Panel tilt (°)", 0, 90, 35)
    azimuth = st.slider("Panel azimuth (°)", 0, 360, 180)

API_KEY = "NiW6JjfVhrZdFMiNwsQfNVuEveL67iy2Jmq9Gopz"

if uploaded_file and st.button("生成并下载 CSV"):
    try:
        # 1) 负荷：5 分钟 -> 多伦多本地按小时聚合
        load_hourly = parse_alectra_xml_hourly(uploaded_file)

        # 2) 解析年份
        if "-" in year_range:
            start_year, end_year = map(int, year_range.split("-"))
        else:
            start_year = end_year = int(year_range)

        # 3) PV：小时级（多伦多本地），并计算 epoch
        pv_hourly = pvwatts_hourly_with_epoch(
            lat, lon, start_year, end_year,
            system_capacity_kw=system_capacity_kw, tilt=tilt, azimuth=azimuth,
            api_key=API_KEY
        )

        # 4) 合并（按 epoch）
        merged = pd.merge(load_hourly, pv_hourly[["epoch", "ac_kWh", "多伦多时间"]], on="epoch", how="inner", suffixes=("", "_pv"))
        # 统一“多伦多时间”列（用 PV 侧或 load 侧均可；两者小时应一致）
        merged["多伦多时间"] = merged["多伦多时间_pv"]
        merged = merged.drop(columns=["多伦多时间_pv"])

        # 5) 重命名列（含容量），并计算净负荷
        cap_int = int(round(system_capacity_kw))
        pv_col = f"{cap_int}kW 发电量"
        net_col = f"{cap_int}kW 净负荷"
        merged = merged.rename(columns={"ac_kWh": pv_col})

        merged[net_col] = merged["load kWh"] - merged[pv_col]

        # 6) 仅保留指定 5 列并排序
        out_df = merged[["epoch", "多伦多时间", "load kWh", pv_col, net_col]].sort_values("epoch").reset_index(drop=True)

        # 7) 导出
        output = BytesIO()
        out_df.to_csv(output, index=False)
        st.download_button(
            label="⬇️ 下载合并结果 CSV",
            data=output.getvalue(),
            file_name="merged_hourly_load_pv.csv",
            mime="text/csv"
        )

        st.success("✅ 已生成每小时 CSV（epoch 对齐、包含净负荷）。")

    except Exception as e:
        st.error(f"Error: {e}")
