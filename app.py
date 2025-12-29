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
st.set_page_config(page_title="員工KPI考核系統 (流程優化版)", layout="wide", page_icon="📈")

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
        defaults = {
            "tasks": ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at'],
            "employees": ["email", "name", "password", "department", "manager_email", "role"],
            "departments": ["dept_id", "dept_name", "level", "parent_dept_id"]
        }
        for i in range(3):
            try:
                ws = None
                if table_name == "employees": ws = self.ws_emp
                elif table_name == "departments": ws = self.ws_dept
                elif table_name == "tasks": ws = self.ws_tasks
                
                if ws:
                    data = ws.get_all_records()
                    df = pd.DataFrame(data)
                    if df.empty and table_name in defaults: return pd.DataFrame(columns=defaults[table_name])
                    if table_name == "tasks" and "task_id" not in df.columns:
                        ws.clear(); ws.append_row(defaults["tasks"])
                        return pd.DataFrame(columns=defaults["tasks"])
                    return df
            except APIError: time.sleep(1)
        return pd.DataFrame()

    def batch_update_sheet(self, ws, df, key_col):
        try:
            ws.clear()
            ws.update([df.columns.values.tolist()] + df.values.tolist())
            return True, "更新成功"
        except Exception as e: return False, str(e)

    # --- [更新] 批次新增任務 (支援指定狀態) ---
    def batch_add_tasks(self, df_tasks, initial_status="Draft"):
        try:
            # 防呆檢查
            for idx, row in df_tasks.iterrows():
                try:
                    s_date = pd.to_datetime(row['start_date'])
                    e_date = pd.to_datetime(row['end_date'])
                    if e_date < s_date: return False, f"錯誤: 任務 '{row['task_name']}' 結束日早於開始日"
                except: return False, f"錯誤: 任務 '{row['task_name']}' 日期格式錯誤"

            base_id = int(time.time())
            df_tasks['task_id'] = [f"{base_id}_{i}" for i in range(len(df_tasks))]
            df_tasks['points'] = 0
            df_tasks['status'] = initial_status # 使用傳入的狀態
            df_tasks['progress_pct'] = 0
            df_tasks['progress_desc'] = ""
            df_tasks['manager_comment'] = ""
            df_tasks['created_at'] = str(date.today())
            df_tasks['approved_at'] = ""
            
            df_tasks['start_date'] = df_tasks['start_date'].astype(str)
            df_tasks['end_date'] = df_tasks['end_date'].astype(str)

            cols = ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at']
            for c in cols:
                if c not in df_tasks.columns: df_tasks[c] = ""
            
            # 檢查標題
            current_data = self.ws_tasks.get_all_values()
            if not current_data: self.ws_tasks.append_row(cols)
                
            values = df_tasks[cols].values.tolist()
            self.ws_tasks.append_rows(values)
            return True, f"已新增 {len(values)} 筆任務 (狀態: {initial_status})"
        except Exception as e: return False, str(e)

    def batch_update_tasks_status(self, updates_list):
        try:
            all_tasks = self.get_df("tasks")
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
                if len(row) > 2 and str(row[2]) == str(password):
                    role_val = row[5] if len(row) > 5 else "user"
                    manager_val = row[4] if len(row) > 4 else ""
                    return {"role": role_val, "name": row[1], "email": row[0], "manager": manager_val}
        except: pass
        return None

    def upsert_employee(self, email, name, password, dept, manager, role="user"):
        df = pd.DataFrame([{"email": email, "name": name, "password": password, "department": dept, "manager_email": manager, "role": role}])
        return self.save_employees_from_editor(pd.concat([self.get_df("employees"), df], ignore_index=True).drop_duplicates(subset=['email'], keep='last'))

    def save_employees_from_editor(self, df_new):
        cols = ["email", "name", "password", "department", "manager_email", "role"]
        for c in cols: 
            if c not in df_new.columns: df_new[c] = ""
        df_new = df_new[cols].astype(str)
        return self.batch_update_sheet(self.ws_emp, df_new, "email")

    def batch_import_employees(self, df):
        try:
            current = self.get_df("employees")
            if current.empty: current = pd.DataFrame(columns=["email", "name", "password", "department", "manager_email", "role"])
            df['role'] = 'user'
            rename_map = {"Email": "email", "姓名": "name", "密碼": "password", "單位": "department", "主管Email": "manager_email"}
            df.rename(columns=rename_map, inplace=True)
            combined = pd.concat([current, df], ignore_index=True).drop_duplicates(subset=['email'], keep='last')
            return self.save_employees_from_editor(combined)
        except Exception as e: return False, str(e)

    def save_depts_from_editor(self, df_new):
        cols = ["dept_id", "dept_name", "level", "parent_dept_id"]
        for c in cols: 
            if c not in df_new.columns: df_new[c] = ""
        df_new = df_new[cols].astype(str)
        return self.batch_update_sheet(self.ws_dept, df_new, "dept_id")

    def batch_import_depts(self, df):
        try:
            current = self.get_df("departments")
            if current.empty: current = pd.DataFrame(columns=["dept_id", "dept_name", "level", "parent_dept_id"])
            rename_map = {"部門代號": "dept_id", "部門名稱": "dept_name", "層級": "level", "上層代號": "parent_dept_id"}
            df.rename(columns=rename_map, inplace=True)
            combined = pd.concat([current, df], ignore_index=True).drop_duplicates(subset=['dept_id'], keep='last')
            return self.save_depts_from_editor(combined)
        except Exception as e: return False, str(e)

    def upsert_dept(self, d_id, d_name, level, parent):
        df = pd.DataFrame([{"dept_id": d_id, "dept_name": d_name, "level": level, "parent_dept_id": parent}])
        return self.save_depts_from_editor(pd.concat([self.get_df("departments"), df], ignore_index=True).drop_duplicates(subset=['dept_id'], keep='last'))

@st.cache_resource
def get_db(): return KPIDB()

try: sys = get_db()
except Exception as e: st.error(f"System Error: {e}"); st.stop()

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
    change_password_ui("admin", "admin")
    
    tab1, tab2 = st.tabs(["👥 員工管理", "🏢 組織圖"])
    
    with tab1:
        st.subheader("員工資料維護")
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
                    if ne_email:
                        sys.upsert_employee(ne_email, ne_name, ne_pwd, ne_dept, ne_mgr)
                        st.success("已新增"); time.sleep(1); st.rerun()
                    else: st.error("Email 為必填")

        st.write("▼ 直接在表格修改，勾選「刪除」欄位可移除資料")
        df_emp = sys.get_df("employees")
        if not df_emp.empty:
            df_emp['刪除'] = False 
            cols_order = ['刪除', 'email', 'name', 'password', 'department', 'manager_email', 'role']
            edited_df = st.data_editor(
                df_emp[cols_order],
                column_config={
                    "刪除": st.column_config.CheckboxColumn(help="勾選後按下方儲存即可刪除", default=False),
                    "email": st.column_config.TextColumn(disabled=True)
                },
                use_container_width=True,
                hide_index=True
            )
            
            if st.button("💾 儲存員工變更", type="primary"):
                to_keep = edited_df[edited_df['刪除'] == False].drop(columns=['刪除'])
                succ, msg = sys.save_employees_from_editor(to_keep)
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)
        
        st.divider()
        with st.expander("📂 Excel 批次匯入員工"):
            up = st.file_uploader("上傳 Excel", type=["xlsx"], key="up_e")
            if up and st.button("確認匯入"):
                sys.batch_import_employees(pd.read_excel(up))
                st.success("匯入完成"); st.rerun()

    with tab2:
        st.subheader("組織資料維護")
        with st.expander("➕ 單筆新增部門"):
            with st.form("add_dept"):
                c1, c2 = st.columns(2)
                nd_id = c1.text_input("部門代號"); nd_name = c2.text_input("部門名稱")
                c3, c4 = st.columns(2)
                nd_lv = c3.text_input("層級"); nd_p = c4.text_input("上層代號")
                if st.form_submit_button("新增"):
                    if nd_id:
                        sys.upsert_dept(nd_id, nd_name, nd_lv, nd_p)
                        st.success("已新增"); time.sleep(1); st.rerun()
                    else: st.error("代號必填")

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
    
    tab1, tab2, tab3 = st.tabs(["📝 任務列表", "➕ 批次新增任務", "📖 相關辦法"])

    with tab1:
        st.subheader("我的任務清單")
        df_tasks = sys.get_df("tasks")
        
        if df_tasks.empty:
            st.info("尚無任何任務")
        else:
            my_tasks = df_tasks[df_tasks['owner_email'] == user['email']].copy()
            
            # 分類顯示
            drafts = my_tasks[my_tasks['status'] == 'Draft']
            submitted = my_tasks[my_tasks['status'] == 'Submitted']
            approved = my_tasks[my_tasks['status'] == 'Approved']
            rejected = my_tasks[my_tasks['status'] == 'Rejected']

            # 1. 暫存區 (Draft)
            with st.expander(f"💾 暫存任務 ({len(drafts)})", expanded=True):
                if not drafts.empty:
                    st.dataframe(drafts[['task_name', 'start_date', 'end_date', 'size', 'description']])
                    # 暫存轉送出 (批次)
                    draft_opts = [f"{r['task_name']}" for i, r in drafts.iterrows()]
                    selected_draft = st.multiselect("選擇要送審的任務", draft_opts)
                    if st.button("🚀 送出選取任務"):
                        updates = []
                        for name in selected_draft:
                            tid = drafts[drafts['task_name'] == name].iloc[0]['task_id']
                            updates.append({'task_id': tid, 'status': "Submitted"})
                        if updates:
                            sys.batch_update_tasks_status(updates)
                            st.success("已送出審核"); time.sleep(1); st.rerun()
                else:
                    st.caption("無暫存任務")

            # 2. 送審區 (Submitted)
            with st.expander(f"⏳ 送審中 ({len(submitted)})"):
                if not submitted.empty:
                    st.dataframe(submitted[['task_name', 'start_date', 'end_date', 'size', 'description']])
                else:
                    st.caption("無送審任務")

            # 3. 核可與退回 (Approved / Rejected)
            with st.expander(f"✅ 已核可 / ⚠️ 被退回 ({len(approved) + len(rejected)})"):
                # 被退回
                if not rejected.empty:
                    st.markdown("#### ⚠️ 被退回")
                    for i, r in rejected.iterrows():
                        st.error(f"**{r['task_name']}** | 評語: {r['manager_comment']}")
                        if st.button("重送審核", key=f"re_{r['task_id']}"):
                            sys.batch_update_tasks_status([{'task_id': r['task_id'], 'status': "Submitted"}])
                            st.rerun()
                
                # 已核可 (執行中)
                if not approved.empty:
                    st.markdown("#### ✅ 執行中")
                    for i, r in approved.iterrows():
                        with st.container():
                            st.write(f"**{r['task_name']}** ({r['start_date']}~{r['end_date']}) | 點數: {r['points']}")
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
                            st.divider()

    with tab2:
        st.subheader("批次新增任務")
        st.markdown("填寫完畢後，可選擇 **「僅暫存」** 或 **「直接送審」**。")
        
        default_data = {
            "task_name": [""] * 10,
            "description": [""] * 10,
            "start_date": [date.today()] * 10,
            "end_date": [date.today() + timedelta(days=7)] * 10,
            "size": ["M"] * 10
        }
        input_df = pd.DataFrame(default_data)
        
        edited_tasks = st.data_editor(
            input_df,
            column_config={
                "task_name": "任務名稱",
                "description": st.column_config.TextColumn("說明 (50字內)", max_chars=50),
                "start_date": st.column_config.DateColumn("開始日"),
                "end_date": st.column_config.DateColumn("結束日"),
                "size": st.column_config.SelectboxColumn("預估大小", options=["S", "M", "L", "XL"])
            },
            num_rows="dynamic",
            use_container_width=True
        )
        
        c1, c2 = st.columns(2)
        # 按鈕 1: 存為草稿
        if c1.button("💾 儲存為暫存 (Draft)", type="secondary"):
            valid_tasks = edited_tasks[edited_tasks['task_name'] != ""]
            if not valid_tasks.empty:
                valid_tasks['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(valid_tasks, initial_status="Draft")
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)
            else:
                st.warning("請填寫任務")

        # 按鈕 2: 直接送出
        if c2.button("🚀 暫存並送出 (Submit)", type="primary"):
            valid_tasks = edited_tasks[edited_tasks['task_name'] != ""]
            if not valid_tasks.empty:
                valid_tasks['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(valid_tasks, initial_status="Submitted")
                if succ: st.success(msg); time.sleep(1); st.rerun()
                else: st.error(msg)
            else:
                st.warning("請填寫任務")
        
        st.divider()
        with st.expander("📂 Excel 匯入任務"):
            # 下載範本
            sample_task = pd.DataFrame([{"任務名稱": "專案A", "說明": "第一階段開發", "開始日期": "2025-01-01", "結束日期": "2025-01-31", "大小": "M"}])
            buf3 = io.BytesIO()
            with pd.ExcelWriter(buf3, engine='xlsxwriter') as w: sample_task.to_excel(w, index=False)
            st.download_button("📥 下載任務範本", buf3, "task_template.xlsx")
            
            up_t = st.file_uploader("上傳任務 Excel", type=["xlsx"])
            
            c3, c4 = st.columns(2)
            if c3.button("匯入並暫存"):
                if up_t:
                    df_up = pd.read_excel(up_t)
                    rename_map = {"任務名稱":"task_name", "說明":"description", "開始日期":"start_date", "結束日期":"end_date", "大小":"size"}
                    df_up.rename(columns=rename_map, inplace=True)
                    df_up['owner_email'] = user['email']
                    succ, msg = sys.batch_add_tasks(df_up, initial_status="Draft")
                    if succ: st.success(msg)
                    else: st.error(msg)
            
            if c4.button("匯入並送審"):
                if up_t:
                    df_up = pd.read_excel(up_t)
                    rename_map = {"任務名稱":"task_name", "說明":"description", "開始日期":"start_date", "結束日期":"end_date", "大小":"size"}
                    df_up.rename(columns=rename_map, inplace=True)
                    df_up['owner_email'] = user['email']
                    succ, msg = sys.batch_add_tasks(df_up, initial_status="Submitted")
                    if succ: st.success(msg)
                    else: st.error(msg)

    with tab3:
        st.markdown("### 辦法說明...")

def manager_page():
    user = st.session_state.user
    
    # 待辦提醒 (Alert)
    df_emp = sys.get_df("employees")
    team = df_emp[df_emp['manager_email'] == user['email']]['email'].tolist()
    df_tasks = sys.get_df("tasks")
    pending = df_tasks[df_tasks['owner_email'].isin(team) & (df_tasks['status'] == "Submitted")].copy()
    
    pending_count = len(pending)
    if pending_count > 0:
        st.warning(f"🔔 提醒：您有 **{pending_count}** 筆任務等待審核！")
    else:
        st.success("✅ 目前沒有待審核任務。")

    st.header(f"👨‍💼 主管審核 - {user['name']}")
    
    if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
    ROWS_PER_PAGE = 50

    if pending.empty:
        st.info("目前無待審核案件")
    else:
        st.write(f"待審核總數: {len(pending)} 筆")
        
        total_pages = max(1, (len(pending) - 1) // ROWS_PER_PAGE + 1)
        if st.session_state.page_idx >= total_pages: st.session_state.page_idx = 0
        
        start = st.session_state.page_idx * ROWS_PER_PAGE
        end = start + ROWS_PER_PAGE
        page_data = pending.iloc[start:end].copy()
        
        page_data['審核決定'] = "無動作" 
        page_data['核定等級'] = page_data['size'] 
        page_data['給予點數'] = 0
        page_data['評語'] = ""
        
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
            key=f"editor_{st.session_state.page_idx}"
        )
        
        c1, c2, c3 = st.columns([1, 1, 3])
        if st.session_state.page_idx > 0:
            if c1.button("⬅️ 上一頁"): st.session_state.page_idx -= 1; st.rerun()
        
        if st.session_state.page_idx < total_pages - 1:
            if c2.button("下一頁 ➡️"): st.session_state.page_idx += 1; st.rerun()
            
        if c3.button("✅ 送出本頁審核結果", type="primary"):
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
        df_emp = sys.get_df("employees")
        is_mgr = not df_emp[df_emp['manager_email'] == st.session_state.user['email']].empty
        if is_mgr: manager_page()
        else: employee_page()
