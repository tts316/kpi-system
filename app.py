import streamlit as st
import pandas as pd
from datetime import datetime, date
import time
import io
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# --- 1. 系統設定 ---
st.set_page_config(page_title="員工KPI考核系統 (完整版)", layout="wide", page_icon="📈")

# 點數對照表
POINT_RANGES = {"S": (1, 3), "M": (4, 6), "L": (7, 9), "XL": (10, 12)}

# Email 設定 (請修改這裡，或是建議使用 st.secrets 管理)
# 若留空，系統會使用「模擬模式」顯示通知，不會真寄信
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""      # 例如: your_company_hr@gmail.com
SENDER_PASSWORD = ""   # Google 應用程式密碼

# --- 2. 資料庫核心 ---
class KPIDB:
    def __init__(self):
        self.connect()

    def connect(self):
        try:
            scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            self.client = gspread.authorize(creds)
            sheet_url = st.secrets["sheet_config"]["spreadsheet_url"]
            self.sh = self.client.open_by_url(sheet_url)
            self.ws_emp = self.sh.worksheet("employees")
            self.ws_dept = self.sh.worksheet("departments")
            self.ws_tasks = self.sh.worksheet("tasks")
            self.ws_admin = self.sh.worksheet("system_admin")
        except Exception as e:
            st.error(f"資料庫連線失敗: {e}")
            st.stop()

    def get_df(self, table_name):
        for i in range(3):
            try:
                if table_name == "employees": return pd.DataFrame(self.ws_emp.get_all_records())
                elif table_name == "departments": return pd.DataFrame(self.ws_dept.get_all_records())
                elif table_name == "tasks": return pd.DataFrame(self.ws_tasks.get_all_records())
            except APIError: time.sleep(1)
        return pd.DataFrame()

    def upsert_employee(self, email, name, password, dept, manager, role="user"):
        try:
            try: cell = self.ws_emp.find(email)
            except: time.sleep(1); cell = self.ws_emp.find(email)
            if cell:
                self.ws_emp.batch_update([{'range': f'B{cell.row}:F{cell.row}', 'values': [[name, password, dept, manager, role]]}])
            else:
                self.ws_emp.append_row([email, name, password, dept, manager, role])
            return True, f"員工 {name} 已更新"
        except Exception as e: return False, str(e)

    def batch_import_employees(self, df):
        try:
            count = 0
            for i, r in df.iterrows():
                email = str(r.get("Email", "")).strip()
                if not email: continue
                self.upsert_employee(email, str(r.get("姓名", "")), str(r.get("密碼", email)), str(r.get("單位", "")), str(r.get("主管Email", "")), "user")
                count+=1
            return True, f"已匯入 {count} 筆"
        except Exception as e: return False, str(e)

    def batch_import_depts(self, df):
        try:
            self.ws_dept.clear(); self.ws_dept.append_row(["dept_id", "dept_name", "level", "parent_dept_id"])
            rows = [[r.get("部門代號"), r.get("部門名稱"), r.get("層級"), r.get("上層代號")] for i, r in df.iterrows()]
            self.ws_dept.append_rows(rows)
            return True, f"已重置並匯入 {len(rows)} 筆"
        except Exception as e: return False, str(e)

    def add_task(self, owner, name, desc, s_date, e_date, size):
        try:
            tid = str(int(time.time()))
            self.ws_tasks.append_row([tid, owner, name, desc, str(s_date), str(e_date), size, 0, "Draft", 0, "", "", str(date.today()), ""])
            return True, tid # 回傳 ID 以便後續操作
        except Exception as e: return False, str(e)

    def update_task_status(self, tid, status, points=None, size=None, comment=None):
        try:
            cell = self.ws_tasks.find(str(tid), in_column=1)
            if cell:
                row = cell.row
                self.ws_tasks.update_cell(row, 9, status)
                if points is not None: self.ws_tasks.update_cell(row, 8, points)
                if size is not None: self.ws_tasks.update_cell(row, 7, size)
                if comment is not None: self.ws_tasks.update_cell(row, 12, comment)
                if status == "Approved": self.ws_tasks.update_cell(row, 14, str(date.today()))
                return True, "更新成功"
            return False, "找不到任務"
        except Exception as e: return False, str(e)

    def update_progress(self, tid, pct, desc):
        try:
            cell = self.ws_tasks.find(str(tid), in_column=1)
            if cell:
                self.ws_tasks.update_cell(cell.row, 10, pct)
                self.ws_tasks.update_cell(cell.row, 11, desc)
                return True, "進度已回報"
            return False, "失敗"
        except Exception as e: return False, str(e)

    def verify_user(self, email, password):
        if email == "admin":
            try:
                c = self.ws_admin.find("admin")
                if c and str(self.ws_admin.cell(c.row, 2).value) == password:
                    return {"role": "admin", "name": "管理員", "email": "admin"}
            except: pass
        try:
            c = self.ws_emp.find(email, in_column=1)
            if c:
                row = self.ws_emp.row_values(c.row)
                if str(row[2]) == str(password):
                    return {"role": row[5], "name": row[1], "email": row[0], "manager": row[4]}
        except: pass
        return None

@st.cache_resource
def get_db(): return KPIDB()

try: sys = get_db()
except Exception as e: st.error(f"System Error: {e}"); st.stop()

# --- 輔助函式 ---
def calc_expected_progress(start_str, end_str):
    try:
        s = datetime.strptime(start_str, "%Y-%m-%d").date()
        e = datetime.strptime(end_str, "%Y-%m-%d").date()
        today = date.today()
        if today < s: return 0
        if today > e: return 100
        total = (e - s).days
        if total <= 0: return 100
        return int(((today - s).days / total) * 100)
    except: return 0

def send_notification_email(to_email, subject, content):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print(f"【模擬發信】To: {to_email} | Subject: {subject}")
        return True # 模擬成功
    try:
        msg = MIMEText(content, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"發信失敗: {e}")
        return False

# --- 📊 新增功能：儀表板元件 ---
def render_dashboard(df_user_tasks):
    """繪製個人的 KPI 儀表板"""
    if df_user_tasks.empty:
        st.info("尚無任務數據")
        return

    # 計算統計數據
    total_tasks = len(df_user_tasks)
    approved_tasks = df_user_tasks[df_user_tasks['status'] == 'Approved']
    
    # 總點數 (只算核准的)
    total_points = approved_tasks['points'].sum()
    
    # 進行中任務平均進度
    active_tasks = df_user_tasks[df_user_tasks['status'] == 'Approved'] # 簡化定義：核准即為進行中/已完成
    avg_progress = active_tasks['progress_pct'].mean() if not active_tasks.empty else 0
    
    # 顯示指標
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("本月累計點數", f"{total_points} pts")
    c2.metric("任務總數", total_tasks)
    c3.metric("核准率", f"{int(len(approved_tasks)/total_tasks*100)}%")
    c4.metric("平均執行進度", f"{int(avg_progress)}%")
    
    # 圖表：任務狀態分佈
    st.caption("任務狀態分佈")
    status_counts = df_user_tasks['status'].value_counts()
    st.bar_chart(status_counts, color="#4CAF50")

# --- UI 介面 ---

def login_page():
    st.markdown("## 📈 員工點數制 KPI 系統")
    col1, col2 = st.columns(2)
    with col1:
        email_input = st.text_input("帳號 (Email)")
        password = st.text_input("密碼", type="password")
        if st.button("登入", type="primary"):
            user = sys.verify_user(email_input, password)
            if user:
                st.session_state.user = user
                st.rerun()
            else: st.error("帳號或密碼錯誤")
    with col2: st.info("💡 預設管理員: admin / admin888")

def admin_page():
    st.header("🔧 管理後台")
    tab1, tab2 = st.tabs(["👥 員工管理", "🏢 組織圖"])
    
    with tab1:
        # 下載範本
        sample = pd.DataFrame([{"Email": "u1@co.com", "姓名": "王小明", "密碼": "123", "單位": "業務部", "主管Email": "boss@co.com"}])
        buf = io.BytesIO(); 
        with pd.ExcelWriter(buf, engine='xlsxwriter') as w: sample.to_excel(w, index=False)
        st.download_button("下載員工範本", buf, "emp_template.xlsx")
        
        up = st.file_uploader("匯入員工", type=["xlsx"])
        if up and st.button("確認匯入"):
            succ, msg = sys.batch_import_employees(pd.read_excel(up))
            if succ: st.success(msg)
            else: st.error(msg)
        st.dataframe(sys.get_df("employees"))

    with tab2:
        sample_d = pd.DataFrame([{"部門代號": "D01", "部門名稱": "總經理室", "層級": "總經理室", "上層代號": ""}])
        buf2 = io.BytesIO(); 
        with pd.ExcelWriter(buf2, engine='xlsxwriter') as w: sample_d.to_excel(w, index=False)
        st.download_button("下載組織範本", buf2, "dept_template.xlsx")
        
        up_d = st.file_uploader("匯入組織", type=["xlsx"])
        if up_d and st.button("確認匯入組織"):
            succ, msg = sys.batch_import_depts(pd.read_excel(up_d))
            if succ: st.success(msg)
            else: st.error(msg)
        st.dataframe(sys.get_df("departments"))

def employee_page():
    user = st.session_state.user
    st.header(f"👋 {user['name']} 的工作台")
    
    # --- 歷史查詢篩選器 (置頂) ---
    with st.expander("🔎 篩選月份/年份", expanded=False):
        c1, c2 = st.columns(2)
        sel_year = c1.selectbox("年份", [2024, 2025, 2026], index=1)
        sel_month = c2.selectbox("月份", list(range(1, 13)), index=datetime.now().month-1)
    
    # 準備資料 (預先篩選)
    df_all = sys.get_df("tasks")
    my_tasks = pd.DataFrame()
    if not df_all.empty:
        # 篩選我的任務 & 符合年月的任務 (依開始日期判斷)
        df_all['start_dt'] = pd.to_datetime(df_all['start_date'], errors='coerce')
        my_tasks = df_all[
            (df_all['owner_email'] == user['email']) & 
            (df_all['start_dt'].dt.year == sel_year) & 
            (df_all['start_dt'].dt.month == sel_month)
        ]

    tab1, tab2, tab3 = st.tabs(["📊 KPI 儀表板", "📝 任務管理", "📖 相關辦法"])

    with tab1:
        st.subheader(f"{sel_year}年{sel_month}月 - 績效概覽")
        render_dashboard(my_tasks)

    with tab2:
        col_list, col_add = st.columns([2, 1])
        
        with col_add:
            st.markdown("### ✨ 新增任務")
            with st.form("new_task"):
                name = st.text_input("任務名稱")
                desc = st.text_area("說明")
                c1, c2 = st.columns(2)
                s_date = c1.date_input("開始")
                e_date = c2.date_input("結束")
                size = st.selectbox("自評大小", ["S", "M", "L", "XL"])
                act = st.radio("動作", ["暫存", "送出審核"])
                
                if st.form_submit_button("確認"):
                    succ, res = sys.add_task(user['email'], name, desc, s_date, e_date, size)
                    if succ:
                        if act == "送出審核":
                            sys.update_task_status(res, "Submitted") # res is tid
                            # --- 📧 發送通知給主管 ---
                            mgr_email = user.get('manager', '')
                            if mgr_email:
                                subject = f"【KPI系統】{user['name']} 提交了新任務：{name}"
                                body = f"主管您好，\n{user['name']} 已提交任務「{name}」待您審核。\n請登入系統查看。"
                                send_notification_email(mgr_email, subject, body)
                                st.success("已送出並通知主管！")
                            else:
                                st.success("已送出 (未設定主管Email，無法通知)")
                        else:
                            st.success("已暫存")
                        time.sleep(1); st.rerun()
                    else: st.error(res)

        with col_list:
            st.markdown("### 📋 任務清單")
            if not my_tasks.empty:
                for i, r in my_tasks.iterrows():
                    # 狀態顏色標記
                    status_color = "red" if r['status']=="Rejected" else "green" if r['status']=="Approved" else "orange"
                    with st.expander(f":{status_color}[{r['status']}] {r['task_name']} ({r['size']})"):
                        st.caption(f"📅 {r['start_date']} ~ {r['end_date']}")
                        st.write(r['description'])
                        
                        if r['manager_comment']:
                            st.info(f"主管評語: {r['manager_comment']}")

                        # 進度回報 (僅核准且未過期可回報)
                        if r['status'] == "Approved":
                            exp_p = calc_expected_progress(r['start_date'], r['end_date'])
                            curr_p = r['progress_pct']
                            
                            c1, c2 = st.columns(2)
                            c1.metric("目前進度", f"{curr_p}%")
                            delta_val = curr_p - exp_p
                            c2.metric("預計進度", f"{exp_p}%", delta=delta_val, delta_color="normal")
                            
                            with st.form(f"p_{r['task_id']}"):
                                np = st.slider("進度", 0, 100, int(curr_p))
                                nd = st.text_input("說明", max_chars=50)
                                if st.form_submit_button("回報"):
                                    sys.update_progress(r['task_id'], np, nd)
                                    st.success("OK"); time.sleep(0.5); st.rerun()
                        
                        elif r['status'] in ["Draft", "Rejected"]:
                            if st.button("送出審核", key=f"s_{r['task_id']}"):
                                sys.update_task_status(r['task_id'], "Submitted")
                                mgr_email = user.get('manager', '')
                                if mgr_email:
                                    send_notification_email(mgr_email, f"【KPI】{user['name']} 重送任務", "請審核")
                                st.rerun()
            else:
                st.info("本月尚無任務")

    with tab3:
        show_rules()

def manager_page():
    user = st.session_state.user
    st.header(f"👨‍💼 主管管理台 - {user['name']}")
    
    # 取得部屬
    df_emp = sys.get_df("employees")
    team_emails = df_emp[df_emp['manager_email'] == user['email']]['email'].tolist()
    
    t1, t2, t3 = st.tabs(["✅ 待審核", "📊 團隊總表", "📝 個人任務"])
    
    df_tasks = sys.get_df("tasks")
    
    with t1:
        pending = df_tasks[df_tasks['owner_email'].isin(team_emails) & (df_tasks['status'] == "Submitted")]
        if pending.empty: st.info("無待審案件")
        else:
            for i, r in pending.iterrows():
                with st.container():
                    col_a, col_b = st.columns([3, 1])
                    col_a.markdown(f"**{r['owner_email']}** | {r['task_name']}")
                    col_a.caption(f"{r['start_date']} ~ {r['end_date']} | 申請: {r['size']}")
                    col_a.write(r['description'])
                    
                    with col_b:
                        new_sz = st.selectbox("等級", ["S","M","L","XL"], index=["S","M","L","XL"].index(r['size']), key=f"z_{r['task_id']}")
                        min_p, max_p = POINT_RANGES[new_sz]
                        pts = st.number_input("點數", min_p, max_p, key=f"pt_{r['task_id']}")
                        cmt = st.text_input("評語", key=f"cm_{r['task_id']}")
                        
                        if st.button("核准", key=f"ok_{r['task_id']}"):
                            sys.update_task_status(r['task_id'], "Approved", pts, new_sz, cmt)
                            st.success("已核准"); time.sleep(1); st.rerun()
                        if st.button("退件", key=f"rj_{r['task_id']}"):
                            sys.update_task_status(r['task_id'], "Rejected", comment=cmt)
                            st.warning("已退件"); time.sleep(1); st.rerun()
                    st.divider()

    with t2:
        # --- 歷史查詢 ---
        c1, c2 = st.columns(2)
        q_year = c1.selectbox("查詢年份", [2024, 2025, 2026], index=1)
        q_month = c2.selectbox("查詢月份", list(range(1, 13)), index=datetime.now().month-1)
        
        team_df = df_tasks[df_tasks['owner_email'].isin(team_emails)].copy()
        if not team_df.empty:
            team_df['s_dt'] = pd.to_datetime(team_df['start_date'], errors='coerce')
            # 篩選月份
            team_df = team_df[
                (team_df['s_dt'].dt.year == q_year) & 
                (team_df['s_dt'].dt.month == q_month)
            ]
            
            # --- 團隊儀表板 ---
            st.subheader("團隊績效概況")
            if not team_df.empty:
                # 1. 各成員點數排行
                approved_only = team_df[team_df['status']=="Approved"]
                if not approved_only.empty:
                    pts_rank = approved_only.groupby("owner_email")['points'].sum().sort_values(ascending=False)
                    st.bar_chart(pts_rank)
                else:
                    st.info("本月尚未有核准的點數")

                # 2. 詳細列表
                st.subheader("詳細任務列表")
                team_df['預計%'] = team_df.apply(lambda x: calc_expected_progress(x['start_date'], x['end_date']), axis=1)
                team_df['落後%'] = team_df['progress_pct'] - team_df['預計%']
                
                # Highlight 落後任務
                def highlight_delay(val):
                    color = 'red' if val < -10 else 'black' # 落後超過10%顯示紅字
                    return f'color: {color}'

                display = team_df[['owner_email', 'task_name', 'status', 'points', 'progress_pct', '預計%', '落後%']]
                st.dataframe(display.style.map(highlight_delay, subset=['落後%']))
            else:
                st.info(f"{q_year}年{q_month}月 無資料")

    with t3:
        employee_page()

def show_rules():
    st.markdown("""
    ### 📖 辦法說明
    1. **點數定義**: S(1-3), M(4-6), L(7-9), XL(10-12)
    2. **預計進度**: (今日-開始)/(結束-開始)
    3. **簽核**: 送出 -> 主管核定 -> 開始執行 -> 回報進度
    """)

# --- Entry ---
if 'user' not in st.session_state: st.session_state.user = None
if st.session_state.user is None: login_page()
else:
    role = st.session_state.user['role']
    with st.sidebar:
        st.write(f"登入: {st.session_state.user['name']}")
        if st.button("登出"): st.session_state.user=None; st.rerun()
    
    if role == "admin": admin_page()
    else:
        df_emp = sys.get_df("employees")
        is_mgr = not df_emp[df_emp['manager_email']==st.session_state.user['email']].empty
        if is_mgr: manager_page()
        else: employee_page()