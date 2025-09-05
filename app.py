import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
from io import BytesIO
import requests


# ---------------- Green Button XML Parser: 5分钟 -> 多伦多本地按小时聚合 ----------------
def parse_alectra_xml_hourly(uploaded_file):
    ns = {"atom": "http://www.w3.org/2005/Atom", "espi": "http://naesb.org/espi"}
    uploaded_file.seek(0)
    tree = ET.parse(uploaded_file)
    root = tree.getroot()

    recs = []
    for block in root.findall(".//atom:entry/atom:content/espi:IntervalBlock", ns):
        for reading in block.findall("espi:IntervalReading", ns):
            start = int(reading.find("espi:timePeriod/espi:start", ns).text)       # epoch秒 (UTC)
            duration = int(reading.find("espi:timePeriod/espi:duration", ns).text) # 预计300秒
            value_wh = float(reading.find("espi:value", ns).text)                  # Wh
            recs.append({"epoch_5min": start, "duration_sec": duration, "load_Wh": value_wh})

    df5 = pd.DataFrame(recs)
    if df5.empty:
        return pd.DataFrame(columns=["epoch", "多伦多时间", "load kWh"])

    # UTC -> 多伦多本地时间
    df5["time_utc"] = pd.to_datetime(df5["epoch_5min"], unit="s", utc=True)
    df5["toronto_time"] = df5["time_utc"].dt.tz_convert("America/Toronto")
    # 对齐到本地整点
    df5["toronto_hour"] = df5["toronto_time"].dt.floor("H")

    # 每小时负荷 (kWh)
    hourly = (
        df5.groupby("toronto_hour", as_index=False)["load_Wh"].sum()
        .rename(columns={"load_Wh": "load_Wh_sum"})
    )
    hourly["load kWh"] = hourly["load_Wh_sum"] / 1000.0

    # 该小时起点对应的 epoch(UTC秒)
    hourly["epoch"] = (hourly["toronto_hour"].dt.tz_convert("UTC").view("int64") // 10**9)

    hourly = hourly.rename(columns={"toronto_hour": "多伦多时间"})
    return hourly[["epoch", "多伦多时间", "load kWh"]]


# ---------------- PVWatts：小时（多伦多本地）-> 处理DST -> epoch ----------------
def pvwatts_hourly_with_epoch(lat, lon, start_year, end_year,
                              system_capacity_kw=300.0, tilt=35, azimuth=180,
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
    outs = r.json()["outputs"]
    ac = outs["ac"]  # 8760 小时的 kWh

    # 用 2001 年（非闰年）的多伦多本地小时序列作为模板（带时区）
    base = pd.DataFrame({
        "toronto_time_base": pd.date_range("2001-01-01", periods=8760, freq="H", tz="America/Toronto"),
        "ac_kWh": ac
    })

    # 为避免 DST 模糊：先去时区 -> 替换年份 -> 再本地化(带参数) -> 转UTC -> epoch
    base["local_naive"] = base["toronto_time_base"].dt.tz_localize(None)

    pieces = []
    for year in range(start_year, end_year + 1):
        dfy = base.copy()
        dfy["local_naive_year"] = dfy["local_naive"].apply(lambda d: d.replace(year=year))
        # 关键：ambiguous='infer' 自动推断回表的两次 01:00；nonexistent='shift_forward' 处理春季跳时缺口
        dfy["多伦多时间"] = pd.to_datetime(dfy["local_naive_year"]).dt.tz_localize(
            "America/Toronto", ambiguous="infer", nonexistent="shift_forward"
        )
        dfy["epoch"] = (dfy["多伦多时间"].dt.tz_convert("UTC").view("int64") // 10**9)
        dfy["year"] = year
        pieces.append(dfy[["epoch", "多伦多时间", "ac_kWh", "year"]])

    return pd.concat(pieces, ignore_index=True)


# ---------------- Streamlit (仅导出CSV) ----------------
st.title("⚡ 每小时负荷 + 光伏发电（epoch 对齐 / 仅生成文件）")

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
        # 1) 负荷（5分钟汇总到多伦多本地每小时）
        load_hourly = parse_alectra_xml_hourly(uploaded_file)

        # 2) 年份
        if "-" in year_range:
            start_year, end_year = map(int, year_range.split("-"))
        else:
            start_year = end_year = int(year_range)

        # 3) PV（处理DST→epoch）
        pv_hourly = pvwatts_hourly_with_epoch(
            lat, lon, start_year, end_year,
            system_capacity_kw=system_capacity_kw, tilt=tilt, azimuth=azimuth,
            api_key=API_KEY
        )

        # 4) 合并（按 epoch）
        merged = pd.merge(
            load_hourly, pv_hourly[["epoch", "多伦多时间", "ac_kWh"]],
            on="epoch", how="inner", suffixes=("", "_pv")
        )
        merged["多伦多时间"] = merged["多伦多时间"]  # 保留PV侧本地小时起点（与负荷聚合一致）

        # 5) 重命名 + 计算净负荷
        cap_int = int(round(system_capacity_kw))
        gen_col = f"{cap_int}kW 发电量"
        net_col = f"{cap_int}kW 净负荷"

        merged = merged.rename(columns={"ac_kWh": gen_col})
        merged[net_col] = merged["load kWh"] - merged[gen_col]

        # 6) 只保留指定 5 列并排序
        out_df = merged[["epoch", "多伦多时间", "load kWh", gen_col, net_col]].sort_values("epoch").reset_index(drop=True)

        # 7) 导出 CSV
        buf = BytesIO()
        out_df.to_csv(buf, index=False)
        st.download_button(
            label="⬇️ 下载合并结果 CSV",
            data=buf.getvalue(),
            file_name="merged_hourly_load_pv.csv",
            mime="text/csv"
        )
        st.success("✅ 已生成每小时 CSV（epoch 对齐、包含净负荷）。")

    except Exception as e:
        st.error(f"Error: {e}")
