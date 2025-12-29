import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import time
import io
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# --- 1. 系統設定 ---
st.set_page_config(page_title="員工KPI考核系統 (高效版)", layout="wide", page_icon="📈")

POINT_RANGES = {"S": (1, 3), "M": (4, 6), "L": (7, 9), "XL": (10, 12)}

# Email 設定
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""      
SENDER_PASSWORD = ""   

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
            st.error(f"連線失敗: {e}")
            st.stop()

    def get_df(self, table_name):
        for i in range(3):
            try:
                if table_name == "employees": return pd.DataFrame(self.ws_emp.get_all_records())
                elif table_name == "departments": return pd.DataFrame(self.ws_dept.get_all_records())
                elif table_name == "tasks": return pd.DataFrame(self.ws_tasks.get_all_records())
            except APIError: time.sleep(1)
        return pd.DataFrame()

    # --- 批次寫入通用函式 ---
    def batch_update_sheet(self, ws, df, key_col):
        try:
            # 讀取現有資料建立 Map
            current = ws.get_all_records()
            # 假設 key 是字串
            key_map = {str(r[key_col]): i+2 for i, r in enumerate(current)} 
            
            # 這裡簡化邏輯：為了確保資料一致性與處理刪除/修改，
            # 我們採用「全量覆蓋」或「Append」策略比較安全，但在 Google Sheet API 限制下，
            # 若資料量不大，清空重寫是最乾淨的 (除了 Admin 表)。
            # 考慮到保留 ID 不變，我們採用：清空 -> 寫入 Header -> 寫入新 DF
            
            ws.clear()
            ws.update([df.columns.values.tolist()] + df.values.tolist())
            return True, "更新成功"
        except Exception as e: return False, str(e)

    # --- 員工管理 ---
    def save_employees_from_editor(self, df_new):
        # 確保欄位順序
        cols = ["email", "name", "password", "department", "manager_email", "role"]
        # 補齊欄位
        for c in cols:
            if c not in df_new.columns: df_new[c] = ""
        # 轉成字串避免錯誤
        df_new = df_new[cols].astype(str)
        return self.batch_update_sheet(self.ws_emp, df_new, "email")

    def batch_import_employees(self, df):
        try:
            current = self.get_df("employees")
            # 合併
            df['role'] = 'user'
            # 簡單處理：append
            combined = pd.concat([current, df], ignore_index=True).drop_duplicates(subset=['Email'], keep='last')
            # Mapping columns if needed, here assume template matches
            # 需對應欄位名稱: Excel中文 -> DB英文
            rename_map = {"Email": "email", "姓名": "name", "密碼": "password", "單位": "department", "主管Email": "manager_email"}
            combined.rename(columns=rename_map, inplace=True)
            return self.save_employees_from_editor(combined)
        except Exception as e: return False, str(e)

    # --- 組織管理 ---
    def save_depts_from_editor(self, df_new):
        cols = ["dept_id", "dept_name", "level", "parent_dept_id"]
        for c in cols: 
            if c not in df_new.columns: df_new[c] = ""
        df_new = df_new[cols].astype(str)
        return self.batch_update_sheet(self.ws_dept, df_new, "dept_id")

    def batch_import_depts(self, df):
        try:
            current = self.get_df("departments")
            rename_map = {"部門代號": "dept_id", "部門名稱": "dept_name", "層級": "level", "上層代號": "parent_dept_id"}
            df.rename(columns=rename_map, inplace=True)
            combined = pd.concat([current, df], ignore_index=True).drop_duplicates(subset=['dept_id'], keep='last')
            return self.save_depts_from_editor(combined)
        except Exception as e: return False, str(e)

    # --- 任務管理 ---
    def batch_add_tasks(self, df_tasks):
        try:
            # 補上系統欄位
            df_tasks['task_id'] = df_tasks.apply(lambda x: str(int(time.time())) + str(x.name), axis=1) # 避免ID重複
            df_tasks['points'] = 0
            df_tasks['status'] = "Draft"
            df_tasks['progress_pct'] = 0
            df_tasks['progress_desc'] = ""
            df_tasks['manager_comment'] = ""
            df_tasks['created_at'] = str(date.today())
            df_tasks['approved_at'] = ""
            
            # 格式化日期
            df_tasks['start_date'] = df_tasks['start_date'].astype(str)
            df_tasks['end_date'] = df_tasks['end_date'].astype(str)

            # 寫入 (Append)
            values = df_tasks[['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at']].values.tolist()
            self.ws_tasks.append_rows(values)
            return True, f"已新增 {len(values)} 筆任務"
        except Exception as e: return False, str(e)

    def batch_update_tasks_status(self, updates_list):
        # updates_list = [{'task_id':..., 'status':..., 'points':..., 'size':..., 'comment':...}]
        try:
            # 為了效能，這裡先讀取所有資料，在記憶體修改後一次寫回
            all_tasks = self.get_df("tasks")
            # 建立 ID Map
            task_map = {str(r['task_id']): i for i, r in all_tasks.iterrows()}
            
            for up in updates_list:
                tid = str(up['task_id'])
                if tid in task_map:
                    idx = task_map[tid]
                    all_tasks.at[idx, 'status'] = up['status']
                    if 'points' in up: all_tasks.at[idx, 'points'] = up['points']
                    if 'size' in up: all_tasks.at[idx, 'size'] = up['size']
                    if 'comment' in up: all_tasks.at[idx, 'manager_comment'] = up['comment']
                    if up['status'] == "Approved": all_tasks.at[idx, 'approved_at'] = str(date.today())

            # 寫回
            return self.batch_update_sheet(self.ws_tasks, all_tasks, "task_id")
        except Exception as e: return False, str(e)

    def update_progress(self, tid, pct, desc):
        try:
            cell = self.ws_tasks.find(str(tid), in_column=1)
            if cell:
                self.ws_tasks.update_cell(cell.row, 10, pct)
                self.ws_tasks.update_cell(cell.row, 11, desc)
                return True, "成功"
            return False, "失敗"
        except: return False, "Error"

    # --- 密碼修改 ---
    def change_password(self, email, new_password, role="user"):
        try:
            if role == "admin":
                cell = self.ws_admin.find("admin", in_column=1)
                if cell: self.ws_admin.update_cell(cell.row, 2, new_password)
            else:
                cell = self.ws_emp.find(email, in_column=1)
                if cell: self.ws_emp.update_cell(cell.row, 3, new_password)
            return True, "密碼已修改"
        except Exception as e: return False, str(e)

    # --- 登入驗證 ---
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
        s = datetime.strptime(str(start_str), "%Y-%m-%d").date()
        e = datetime.strptime(str(end_str), "%Y-%m-%d").date()
        today = date.today()
        if today < s: return 0
        if today > e: return 100
        total = (e - s).days
        if total <= 0: return 100
        return int(((today - s).days / total) * 100)
    except: return 0

# --- UI 介面 ---

def login_page():
    st.markdown("## 📈 員工點數制 KPI 系統")
    # 移除預設提示
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

def change_password_ui(role, email):
    with st.expander("🔑 修改密碼"):
        new_p = st.text_input("新密碼", type="password", key="new_p")
        cfm_p = st.text_input("確認新密碼", type="password", key="cfm_p")
        if st.button("確認修改"):
            if new_p == cfm_p and new_p:
                succ, msg = sys.change_password(email, new_p, role)
                if succ: st.success(msg)
                else: st.error(msg)
            else: st.error("密碼不一致或為空")

def admin_page():
    st.header("🔧 管理後台")
    change_password_ui("admin", "admin") # 管理員改密碼
    
    tab1, tab2 = st.tabs(["👥 員工管理", "🏢 組織圖"])
    
    with tab1:
        st.subheader("員工資料維護")
        # 1. 單筆新增
        with st.expander("➕ 單筆新增員工"):
            with st.form("add_emp"):
                c1, c2, c3 = st.columns(3)
                ne_email = c1.text_input("Email")
                ne_name = c2.text_input("姓名")
                ne_dept = c3.text_input("單位")
                c4, c5 = st.columns(2)
                ne_pwd = c4.text_input("預設密碼", value="1234")
                ne_mgr = c5.text_input("主管Email")
                if st.form_submit_button("新增"):
                    sys.upsert_employee(ne_email, ne_name, ne_pwd, ne_dept, ne_mgr)
                    st.success("已新增，請重新整理表格"); time.sleep(1); st.rerun()

        # 2. 表格編輯與刪除
        st.write("▼ 直接在表格修改，勾選「刪除」欄位可移除資料")
        df_emp = sys.get_df("employees")
        if not df_emp.empty:
            df_emp['刪除'] = False # 增加刪除勾選欄
            # 調整欄位順序顯示
            cols_order = ['刪除', 'email', 'name', 'password', 'department', 'manager_email', 'role']
            # 使用 data_editor
            edited_df = st.data_editor(
                df_emp[cols_order],
                column_config={
                    "刪除": st.column_config.CheckboxColumn(help="勾選後按下方儲存即可刪除", default=False),
                    "email": st.column_config.TextColumn(disabled=True) # Email 為 Key 不可改
                },
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic" # 允許直接在下方新增
            )
            
            if st.button("💾 儲存員工變更", type="primary"):
                # 處理刪除
                to_keep = edited_df[edited_df['刪除'] == False].drop(columns=['刪除'])
                succ, msg = sys.save_employees_from_editor(to_keep)
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)
        
        st.divider()
        # 3. 批次匯入
        with st.expander("📂 Excel 批次匯入員工"):
            up = st.file_uploader("上傳 Excel", type=["xlsx"], key="up_e")
            if up and st.button("確認匯入"):
                sys.batch_import_employees(pd.read_excel(up))
                st.success("匯入完成"); st.rerun()

    with tab2:
        st.subheader("組織資料維護")
        # 邏輯同員工管理
        with st.expander("➕ 單筆新增部門"):
            with st.form("add_dept"):
                c1, c2 = st.columns(2)
                nd_id = c1.text_input("部門代號"); nd_name = c2.text_input("部門名稱")
                c3, c4 = st.columns(2)
                nd_lv = c3.text_input("層級"); nd_p = c4.text_input("上層代號")
                if st.form_submit_button("新增"):
                    sys.upsert_dept(nd_id, nd_name, nd_lv, nd_p)
                    st.success("已新增"); time.sleep(1); st.rerun()

        df_dept = sys.get_df("departments")
        if not df_dept.empty:
            df_dept['刪除'] = False
            cols_order = ['刪除', 'dept_id', 'dept_name', 'level', 'parent_dept_id']
            edited_dept = st.data_editor(
                df_dept[cols_order],
                column_config={
                    "刪除": st.column_config.CheckboxColumn(default=False),
                    "dept_id": st.column_config.TextColumn(disabled=True)
                },
                use_container_width=True, 
                hide_index=True
            )
            if st.button("💾 儲存組織變更"):
                to_keep = edited_dept[edited_dept['刪除'] == False].drop(columns=['刪除'])
                succ, msg = sys.save_depts_from_editor(to_keep)
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)

        with st.expander("📂 Excel 批次匯入組織"):
            up_d = st.file_uploader("上傳 Excel", type=["xlsx"], key="up_d")
            if up_d and st.button("確認匯入組織"):
                sys.batch_import_depts(pd.read_excel(up_d))
                st.success("匯入完成"); st.rerun()

def employee_page():
    user = st.session_state.user
    st.header(f"👋 {user['name']}")
    change_password_ui("user", user['email'])
    
    tab1, tab2, tab3 = st.tabs(["📝 任務管理", "➕ 批次新增任務", "📖 相關辦法"])

    with tab1:
        st.subheader("我的任務列表")
        df_tasks = sys.get_df("tasks")
        if not df_tasks.empty:
            my_tasks = df_tasks[df_tasks['owner_email'] == user['email']]
            for i, r in my_tasks.iterrows():
                # 顏色標記
                color = "green" if r['status']=="Approved" else "red" if r['status']=="Rejected" else "blue"
                with st.expander(f":{color}[{r['status']}] {r['task_name']} ({r['size']})"):
                    st.write(f"📅 {r['start_date']} ~ {r['end_date']} | 📌 說明: {r['description']}")
                    if r['manager_comment']: st.info(f"主管評語: {r['manager_comment']}")
                    
                    if r['status'] == "Approved":
                        exp = calc_expected_progress(r['start_date'], r['end_date'])
                        c1, c2 = st.columns(2)
                        c1.metric("目前進度", f"{r['progress_pct']}%")
                        c2.metric("預計進度", f"{exp}%", delta=r['progress_pct']-exp)
                        with st.form(f"p_{r['task_id']}"):
                            np = st.slider("更新進度", 0, 100, int(r['progress_pct']))
                            nd = st.text_input("回報說明", max_chars=50)
                            if st.form_submit_button("回報"):
                                sys.update_progress(r['task_id'], np, nd)
                                st.rerun()
                    elif r['status'] in ["Draft", "Rejected"]:
                        if st.button("送出審核", key=f"s_{r['task_id']}"):
                            sys.update_task_status(r['task_id'], "Submitted")
                            st.success("已送出"); time.sleep(1); st.rerun()
        else: st.info("尚無任務")

    with tab2:
        st.subheader("批次新增任務")
        st.markdown("請在下方表格輸入任務資料 (一次可輸入多筆)，確認無誤後按「批次送出」。")
        
        # 建立預設空表格 (10列)
        default_data = {
            "task_name": [""] * 10,
            "description": [""] * 10,
            "start_date": [date.today()] * 10,
            "end_date": [date.today() + timedelta(days=7)] * 10,
            "size": ["M"] * 10
        }
        input_df = pd.DataFrame(default_data)
        
        # 表格編輯器
        edited_tasks = st.data_editor(
            input_df,
            column_config={
                "task_name": "任務名稱",
                "description": "說明",
                "start_date": st.column_config.DateColumn("開始日"),
                "end_date": st.column_config.DateColumn("結束日"),
                "size": st.column_config.SelectboxColumn("預估大小", options=["S", "M", "L", "XL"])
            },
            num_rows="dynamic", # 允許新增更多列
            use_container_width=True
        )
        
        col_btn1, col_btn2 = st.columns([1, 4])
        if col_btn1.button("🚀 批次送出 (暫存)", type="primary"):
            # 過濾掉沒填名稱的空行
            valid_tasks = edited_tasks[edited_tasks['task_name'] != ""]
            if not valid_tasks.empty:
                valid_tasks['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(valid_tasks)
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)
            else:
                st.warning("請至少填寫一筆任務名稱")
        
        st.divider()
        with st.expander("📂 Excel 匯入任務"):
            st.caption("欄位: 任務名稱, 說明, 開始日期(YYYY-MM-DD), 結束日期(YYYY-MM-DD), 大小(S/M/L/XL)")
            up_t = st.file_uploader("上傳任務 Excel", type=["xlsx"])
            if up_t and st.button("確認匯入任務"):
                df_up = pd.read_excel(up_t)
                # 簡單欄位對應
                rename_map = {"任務名稱":"task_name", "說明":"description", "開始日期":"start_date", "結束日期":"end_date", "大小":"size"}
                df_up.rename(columns=rename_map, inplace=True)
                df_up['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(df_up)
                if succ: st.success(msg)
                else: st.error(msg)

    with tab3:
        st.markdown("### 辦法說明...")

def manager_page():
    user = st.session_state.user
    st.header(f"👨‍💼 主管審核 - {user['name']}")
    
    # 分頁控制 (Pagination)
    if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
    ROWS_PER_PAGE = 50

    # 取得部屬 & 任務
    df_emp = sys.get_df("employees")
    team = df_emp[df_emp['manager_email'] == user['email']]['email'].tolist()
    df_tasks = sys.get_df("tasks")
    
    # 篩選待審核
    pending = df_tasks[df_tasks['owner_email'].isin(team) & (df_tasks['status'] == "Submitted")].copy()
    
    if pending.empty:
        st.info("目前無待審核案件")
    else:
        st.write(f"待審核總數: {len(pending)} 筆")
        
        # 分頁邏輯
        total_pages = max(1, (len(pending) - 1) // ROWS_PER_PAGE + 1)
        # 確保頁碼不超標
        if st.session_state.page_idx >= total_pages: st.session_state.page_idx = 0
        
        start = st.session_state.page_idx * ROWS_PER_PAGE
        end = start + ROWS_PER_PAGE
        page_data = pending.iloc[start:end].copy()
        
        # 準備編輯用表格
        # 增加「審核決定」欄位
        page_data['審核決定'] = "無動作" # 預設
        # 預設主管核定等級 = 申請等級
        page_data['核定等級'] = page_data['size'] 
        page_data['給予點數'] = 0
        page_data['評語'] = ""
        
        # 顯示欄位
        display_cols = ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', '核定等級', '給予點數', '評語', '審核決定']
        
        edited_review = st.data_editor(
            page_data[display_cols],
            column_config={
                "task_id": st.column_config.TextColumn(disabled=True),
                "owner_email": st.column_config.TextColumn("申請人", disabled=True),
                "task_name": st.column_config.TextColumn("任務", disabled=True),
                "description": st.column_config.TextColumn("說明", disabled=True),
                "size": st.column_config.TextColumn("申請等級", disabled=True),
                "核定等級": st.column_config.SelectboxColumn("核定等級", options=["S", "M", "L", "XL"], required=True),
                "給予點數": st.column_config.NumberColumn("點數", min_value=0, max_value=12, required=True),
                "審核決定": st.column_config.SelectboxColumn("決定", options=["無動作", "核准 (Approve)", "退件 (Reject)"], required=True)
            },
            use_container_width=True,
            hide_index=True,
            key=f"editor_{st.session_state.page_idx}" # Key 隨頁碼變動以重置狀態
        )
        
        # 按鈕區
        c1, c2, c3 = st.columns([1, 1, 3])
        if st.session_state.page_idx > 0:
            if c1.button("⬅️ 上一頁"): st.session_state.page_idx -= 1; st.rerun()
        
        if st.session_state.page_idx < total_pages - 1:
            if c2.button("下一頁 ➡️"): st.session_state.page_idx += 1; st.rerun()
            
        if c3.button("✅ 送出本頁審核結果", type="primary"):
            # 處理資料
            updates = []
            for i, r in edited_review.iterrows():
                decision = r['審核決定']
                if decision == "核准 (Approve)":
                    updates.append({
                        "task_id": r['task_id'],
                        "status": "Approved",
                        "size": r['核定等級'],
                        "points": r['給予點數'],
                        "comment": r['評語']
                    })
                elif decision == "退件 (Reject)":
                    updates.append({
                        "task_id": r['task_id'],
                        "status": "Rejected",
                        "comment": r['評語']
                    })
            
            if updates:
                succ, msg = sys.batch_update_tasks_status(updates)
                if succ: st.success(f"已處理 {len(updates)} 筆"); time.sleep(1); st.rerun()
                else: st.error(msg)
            else:
                st.warning("您沒有對任何任務做出核准或退件的決定。")

# --- 主程式入口 ---
if 'user' not in st.session_state: st.session_state.user = None

if st.session_state.user is None:
    login_page()
else:
    role = st.session_state.user['role']
    with st.sidebar:
        st.write(f"👤 {st.session_state.user['name']}")
        if st.button("登出"): st.session_state.user = None; st.rerun()
    
    if role == "admin": admin_page()
    else:
        # 主管也是員工，這裡簡單邏輯：若有下屬則為主管介面 (可再細分 Tab 包含個人任務)
        df_emp = sys.get_df("employees")
        is_mgr = not df_emp[df_emp['manager_email'] == st.session_state.user['email']].empty
        if is_mgr: manager_page()
        else: employee_page()
