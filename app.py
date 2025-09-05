import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
from io import BytesIO
import requests

# ============== 1) Green Button XML -> 多伦多本地每小时 ==============
def parse_alectra_xml_hourly(uploaded_file):
    ns = {"atom": "http://www.w3.org/2005/Atom", "espi": "http://naesb.org/espi"}
    uploaded_file.seek(0)
    root = ET.parse(uploaded_file).getroot()

    recs = []
    for block in root.findall(".//atom:entry/atom:content/espi:IntervalBlock", ns):
        for rd in block.findall("espi:IntervalReading", ns):
            start = int(rd.find("espi:timePeriod/espi:start", ns).text)       # epoch (s, UTC)
            val_wh = float(rd.find("espi:value", ns).text)                    # Wh
            recs.append({"epoch_5min": start, "load_Wh": val_wh})

    df5 = pd.DataFrame(recs)
    if df5.empty:
        return pd.DataFrame(columns=["epoch","多伦多时间","load kWh","month","day","hour","seq"])

    # UTC -> Toronto，并对齐到本地整点
    df5["utc_time"] = pd.to_datetime(df5["epoch_5min"], unit="s", utc=True)
    df5["toronto_time"] = df5["utc_time"].dt.tz_convert("America/Toronto")
    df5["toronto_hour"] = df5["toronto_time"].dt.floor("H")

    # 汇总到小时（kWh）
    hourly = df5.groupby("toronto_hour", as_index=False)["load_Wh"].sum()
    hourly["load kWh"] = hourly["load_Wh"] / 1000.0

    # 为解决回表 1:00 出现两次：按 (date, hour) 生成序号 seq（1,2）
    hourly["date"] = hourly["toronto_hour"].dt.date
    hourly["hour"] = hourly["toronto_hour"].dt.hour
    hourly["seq"] = hourly.groupby(["date","hour"]).cumcount() + 1

    # 导出所需字段
    hourly["多伦多时间"] = hourly["toronto_hour"]
    hourly["epoch"] = (hourly["多伦多时间"].dt.tz_convert("UTC").view("int64") // 10**9)
    hourly["month"] = hourly["多伦多时间"].dt.month
    hourly["day"]   = hourly["多伦多时间"].dt.day

    out = hourly[["epoch","多伦多时间","load kWh","month","day","hour","seq"]].sort_values("epoch").reset_index(drop=True)
    return out

# ============== 2) PVWatts（模板年 2001）-> 提取 (month, day, hour, seq) ==============
def pvwatts_template(lat, lon, system_capacity_kw=300.0, tilt=35, azimuth=180,
                     losses=14, api_key="YOUR_API_KEY"):
    url = "https://developer.nrel.gov/api/pvwatts/v6.json"
    params = {
        "api_key": api_key, "lat": lat, "lon": lon,
        "system_capacity": system_capacity_kw,
        "azimuth": azimuth, "tilt": tilt,
        "array_type": 1, "module_type": 1,
        "losses": losses, "timeframe": "hourly",
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    ac = r.json()["outputs"]["ac"]  # 8760 小时 kWh

    # 2001 年（模板年，非闰年）多伦多本地小时序列
    base = pd.DataFrame({
        "多伦多时间": pd.date_range("2001-01-01", periods=8760, freq="H", tz="America/Toronto"),
        "ac_kWh": ac
    })

    # 为处理秋季回表重复小时：按 (date, hour) 生成 seq
    base["date"] = base["多伦多时间"].dt.date
    base["hour"] = base["多伦多时间"].dt.hour
    base["seq"]  = base.groupby(["date","hour"]).cumcount() + 1
    base["month"] = base["多伦多时间"].dt.month
    base["day"]   = base["多伦多时间"].dt.day

    # 模板仅需 (month, day, hour, seq) -> ac_kWh
    tpl = base[["month","day","hour","seq","ac_kWh"]]
    return tpl

# ============== 3) Streamlit（仅导出 CSV） ==============
st.title("⚡ 每小时负荷 + 光伏发电（DST 安全映射 / 仅导出 CSV）")

uploaded_file = st.file_uploader("上传 Alectra Green Button XML", type=["xml"])

with st.sidebar:
    st.header("PV 参数")
    lat = st.number_input("Latitude", value=43.653, format="%.6f")
    lon = st.number_input("Longitude", value=-79.383, format="%.6f")
    system_capacity_kw = st.number_input("System capacity (kW)", min_value=1.0, value=300.0, step=10.0)
    tilt = st.slider("Panel tilt (°)", 0, 90, 35)
    azimuth = st.slider("Panel azimuth (°)", 0, 360, 180)

API_KEY = "NiW6JjfVhrZdFMiNwsQfNVuEveL67iy2Jmq9Gopz"

if uploaded_file and st.button("生成并下载 CSV"):
    try:
        # 1) 负荷（按多伦多本地整点汇总，并带上 month/day/hour/seq）
        load_hr = parse_alectra_xml_hourly(uploaded_file)

        # 2) PV 模板（2001年），按 (month, day, hour, seq) 映射
        pv_tpl = pvwatts_template(
            lat, lon,
            system_capacity_kw=system_capacity_kw,
            tilt=tilt, azimuth=azimuth,
            api_key=API_KEY
        )

        # 3) 合并：用 (month, day, hour, seq) 对齐，完全避开 DST 二义性
        merged = pd.merge(
            load_hr, pv_tpl,
            on=["month","day","hour","seq"],
            how="left"  # 有些极端边界如缺失/额外小时，用 left 保留负荷侧记录
        )

        # 4) 列命名与净负荷
        cap_int = int(round(system_capacity_kw))
        gen_col = f"{cap_int}kW 发电量"
        net_col = f"{cap_int}kW 净负荷"
        merged = merged.rename(columns={"ac_kWh": gen_col})
        merged[gen_col] = merged[gen_col].fillna(0.0)      # 保险：若极少数小时无映射，则视为0
        merged[net_col] = merged["load kWh"] - merged[gen_col]

        # 5) 只保留 5 列并导出
        out_df = merged[["epoch","多伦多时间","load kWh", gen_col, net_col]].sort_values("epoch").reset_index(drop=True)

        buf = BytesIO()
        out_df.to_csv(buf, index=False)
        st.download_button(
            "⬇️ 下载合并结果 CSV",
            data=buf.getvalue(),
            file_name="merged_hourly_load_pv.csv",
            mime="text/csv"
        )
        st.success("✅ 已生成每小时 CSV（DST 安全、epoch 对齐、包含净负荷）。")

    except Exception as e:
        st.error(f"Error: {e}")
