import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import time
import io
import base64
import requests
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# --- 1. 系統設定 ---
st.set_page_config(page_title="聯成教育員工KPI考核系統", layout="wide", page_icon="📈")

POINT_RANGES = {"S": (1, 3), "M": (4, 6), "L": (7, 9), "XL": (10, 12)}

# QR Code 圖片連結 (來自您的 GitHub)
LINE_QR_CODE_URL = "https://raw.githubusercontent.com/tts316/Resume_System/main/qrcode.png"

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
            self.ws_settings = self.sh.worksheet("system_settings")
        except Exception as e:
            st.error(f"連線失敗: {e}")
            st.stop()

    def get_df(self, table_name):
        defaults = {
            "tasks": ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at'],
            "employees": ["email", "name", "password", "department", "manager_email", "role", "line_token"],
            "departments": ["dept_id", "dept_name", "level", "parent_dept_id"],
            "system_settings": ["key", "value"]
        }
        for i in range(3):
            try:
                ws = None
                if table_name == "employees": ws = self.ws_emp
                elif table_name == "departments": ws = self.ws_dept
                elif table_name == "tasks": ws = self.ws_tasks
                elif table_name == "system_settings": ws = self.ws_settings
                
                if ws:
                    data = ws.get_all_records()
                    df = pd.DataFrame(data)
                    
                    if table_name == "tasks" and not df.empty:
                        df['owner_email'] = df['owner_email'].astype(str).str.strip().str.lower()
                        df['task_id'] = df['task_id'].astype(str).str.strip()
                        df['status'] = df['status'].astype(str).str.strip()
                    if table_name == "employees" and not df.empty:
                        df['email'] = df['email'].astype(str).str.strip().str.lower()
                        df['manager_email'] = df['manager_email'].astype(str).str.strip().str.lower()
                        if 'line_token' not in df.columns: df['line_token'] = ""

                    if df.empty and table_name in defaults: return pd.DataFrame(columns=defaults[table_name])
                    if table_name == "tasks" and "task_id" not in df.columns:
                        ws.clear(); ws.append_row(defaults["tasks"])
                        return pd.DataFrame(columns=defaults["tasks"])
                    return df
            except APIError: time.sleep(1)
        return pd.DataFrame(columns=defaults.get(table_name, []))

    def batch_update_sheet(self, ws, df, key_col):
        try:
            ws.clear()
            ws.update([df.columns.values.tolist()] + df.values.tolist())
            return True, "更新成功"
        except Exception as e: return False, str(e)

    def get_setting(self, key):
        try:
            cell = self.ws_settings.find(key, in_column=1)
            if cell: return self.ws_settings.cell(cell.row, 2).value
            return None
        except: return None

    def update_setting(self, key, value):
        try:
            try: cell = self.ws_settings.find(key, in_column=1)
            except: time.sleep(1); cell = self.ws_settings.find(key, in_column=1)
            
            if cell: self.ws_settings.update_cell(cell.row, 2, value)
            else: self.ws_settings.append_row([key, value])
            return True, "設定已更新"
        except Exception as e: return False, str(e)

    # --- LINE 通知 ---
    def get_user_token(self, email):
        try:
            df = self.get_df("employees")
            user = df[df['email'] == email]
            if not user.empty:
                token = str(user.iloc[0].get('line_token', '')).strip()
                return token if token else None
        except: pass
        return None

    def send_line_notify(self, token, message):
        if not token: return
        try:
            line_token = st.secrets["line_config"]["channel_access_token"]
            url = "https://api.line.me/v2/bot/message/push"
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + line_token
            }
            payload = {
                "to": token, 
                "messages": [{"type": "text", "text": message}]
            }
            requests.post(url, headers=headers, json=payload)
        except Exception as e:
            print(f"LINE 發送失敗: {e}")
        
    def update_line_token(self, email, token):
        try:
            cell = self.ws_emp.find(email, in_column=1)
            if cell:
                self.ws_emp.update_cell(cell.row, 7, token)
                return True, "LINE 設定已更新"
            return False, "找不到使用者"
        except Exception as e: return False, str(e)

    def batch_add_tasks(self, df_tasks, initial_status="Draft"):
        try:
            for idx, row in df_tasks.iterrows():
                try:
                    s_date = pd.to_datetime(row['start_date'])
                    e_date = pd.to_datetime(row['end_date'])
                    if e_date < s_date: return False, f"錯誤: 任務 '{row['task_name']}' 結束日早於開始日"
                except: return False, f"錯誤: 任務 '{row['task_name']}' 日期格式錯誤"

            base_id = int(time.time())
            df_tasks['task_id'] = [f"{base_id}_{i}_{int(time.time()*1000)%1000}" for i in range(len(df_tasks))]
            
            df_tasks['points'] = 0
            df_tasks['status'] = initial_status
            df_tasks['progress_pct'] = 0
            df_tasks['progress_desc'] = ""
            df_tasks['manager_comment'] = ""
            df_tasks['created_at'] = str(date.today())
            df_tasks['approved_at'] = ""
            df_tasks['owner_email'] = df_tasks['owner_email'].astype(str).str.strip().str.lower()
            df_tasks['start_date'] = df_tasks['start_date'].astype(str)
            df_tasks['end_date'] = df_tasks['end_date'].astype(str)

            cols = ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at']
            for c in cols:
                if c not in df_tasks.columns: df_tasks[c] = ""
            
            current_vals = self.ws_tasks.get_all_values()
            if not current_vals: self.ws_tasks.append_row(cols)
            values = df_tasks[cols].values.tolist()
            self.ws_tasks.append_rows(values)

            if initial_status == "Submitted":
                df_emp = self.get_df("employees")
                owner_email = df_tasks['owner_email'].iloc[0]
                user_row = df_emp[df_emp['email'] == owner_email]
                if not user_row.empty:
                    mgr_email = user_row.iloc[0]['manager_email']
                    mgr_token = self.get_user_token(mgr_email)
                    user_name = user_row.iloc[0]['name']
                    if mgr_token:
                        msg = f"【KPI 待審核】\n同仁：{user_name}\n提交了 {len(df_tasks)} 筆新任務，請進入系統審核。"
                        self.send_line_notify(mgr_token, msg)

            return True, f"已新增 {len(values)} 筆任務"
        except Exception as e: return False, str(e)

    def delete_batch_tasks_by_ids(self, task_ids):
        try:
            current = self.ws_tasks.get_all_records()
            str_ids = [str(t).strip() for t in task_ids]
            new_records = [r for r in current if str(r['task_id']).strip() not in str_ids]
            headers = ['task_id', 'owner_email', 'task_name', 'description', 'start_date', 'end_date', 'size', 'points', 'status', 'progress_pct', 'progress_desc', 'manager_comment', 'created_at', 'approved_at']
            final_data = []
            for item in new_records:
                row = [item.get(h, "") for h in headers]
                final_data.append(row)
            self.ws_tasks.clear()
            self.ws_tasks.append_row(headers)
            self.ws_tasks.append_rows(final_data)
            return True, "處理成功"
        except Exception as e: return False, str(e)

    def batch_update_tasks_status(self, updates_list):
        try:
            all_tasks = self.get_df("tasks")
            all_tasks['task_id'] = all_tasks['task_id'].astype(str).str.strip()
            task_map = {str(r['task_id']): i for i, r in all_tasks.iterrows()}
            count = 0
            notify_targets = {} 

            for up in updates_list:
                tid = str(up['task_id']).strip()
                if tid in task_map:
                    idx = task_map[tid]
                    old_status = all_tasks.at[idx, 'status']
                    new_status = up['status']
                    
                    all_tasks.at[idx, 'status'] = new_status
                    if 'points' in up: all_tasks.at[idx, 'points'] = up['points']
                    if 'size' in up: all_tasks.at[idx, 'size'] = up['size']
                    if 'comment' in up: all_tasks.at[idx, 'manager_comment'] = up['comment']
                    if new_status == "Approved": all_tasks.at[idx, 'approved_at'] = str(date.today())
                    count += 1

                    owner_email = all_tasks.at[idx, 'owner_email']
                    task_name = all_tasks.at[idx, 'task_name']
                    
                    if old_status == "Draft" and new_status == "Submitted":
                        df_emp = self.get_df("employees")
                        u_row = df_emp[df_emp['email'] == owner_email]
                        if not u_row.empty:
                            mgr_email = u_row.iloc[0]['manager_email']
                            if mgr_email not in notify_targets: notify_targets[mgr_email] = []
                            notify_targets[mgr_email].append(f"同仁送審：{task_name}")

                    if new_status in ["Approved", "Rejected"]:
                        if owner_email not in notify_targets: notify_targets[owner_email] = []
                        st_txt = "✅ 已核准" if new_status == "Approved" else "⚠️ 被退回"
                        notify_targets[owner_email].append(f"任務 {st_txt}：{task_name}")

            if count > 0:
                for email, msgs in notify_targets.items():
                    token = self.get_user_token(email)
                    if token: self.send_line_notify(token, "【KPI 通知】\n" + "\n".join(msgs))

                return self.batch_update_sheet(self.ws_tasks, all_tasks, "task_id")
            return True, "無變更"
        except Exception as e: return False, str(e)

    def update_task_content(self, task_id, name, desc, s_date, e_date, size, status="Submitted"):
        try:
            cell = self.ws_tasks.find(str(task_id).strip(), in_column=1)
            if cell:
                r = cell.row
                self.ws_tasks.update_cell(r, 3, name)
                self.ws_tasks.update_cell(r, 4, desc)
                self.ws_tasks.update_cell(r, 5, str(s_date))
                self.ws_tasks.update_cell(r, 6, str(e_date))
                self.ws_tasks.update_cell(r, 7, size)
                self.ws_tasks.update_cell(r, 9, status)
                self.ws_tasks.update_cell(r, 12, "") 
                
                if status == "Submitted":
                    row_vals = self.ws_tasks.row_values(r)
                    owner = row_vals[1]
                    df_emp = self.get_df("employees")
                    u_row = df_emp[df_emp['email'] == owner]
                    if not u_row.empty:
                        mgr_token = self.get_user_token(u_row.iloc[0]['manager_email'])
                        self.send_line_notify(mgr_token, f"【KPI】同仁 {u_row.iloc[0]['name']} 重送任務：{name}")

                return True, "成功"
            return False, "失敗"
        except Exception as e: return False, str(e)

    def delete_task(self, task_id):
        try:
            cell = self.ws_tasks.find(str(task_id).strip(), in_column=1)
            if cell: self.ws_tasks.delete_rows(cell.row); return True, "成功"
            return False, "失敗"
        except Exception as e: return False, str(e)

    def update_progress(self, tid, pct, desc):
        try:
            cell = self.ws_tasks.find(str(tid).strip(), in_column=1)
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
        email = str(email).strip().lower()
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
                    return {"role": role_val, "name": row[1], "email": str(row[0]).strip().lower(), "manager": manager_val}
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
        df_new['email'] = df_new['email'].str.strip().str.lower()
        df_new['manager_email'] = df_new['manager_email'].str.strip().str.lower()
        return self.batch_update_sheet(self.ws_emp, df_new, "email")

    def batch_import_employees(self, df):
        try:
            current = self.get_df("employees")
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

def get_full_team_emails(manager_email, df_emp):
    l1 = df_emp[df_emp['manager_email'] == manager_email]['email'].tolist()
    l2 = df_emp[df_emp['manager_email'].isin(l1)]['email'].tolist()
    return list(set(l1 + l2))

# --- UI Components ---
def change_password_ui(role, email):
    with st.expander("🔑 帳號設定 (密碼 / LINE通知)"):
        tab1, tab2 = st.tabs(["修改密碼", "設定 LINE 通知"])
        
        with tab1:
            new_p = st.text_input("新密碼", type="password", key="new_p")
            cfm_p = st.text_input("確認新密碼", type="password", key="cfm_p")
            if st.button("確認修改"):
                if new_p == cfm_p and new_p:
                    succ, msg = sys.change_password(email, new_p, role)
                    if succ: st.success(msg)
                    else: st.error(msg)
                else: st.error("密碼不一致或為空")
        
        with tab2:
            st.markdown("### 🔔 LINE 綁定設定")
            # [新增] QR Code 顯示
            st.image(LINE_QR_CODE_URL, width=200, caption="掃描加入官方帳號")
            st.info("請加入官方帳號好友，並傳送您的 Email 進行自動綁定。")
            st.markdown("**官方帳號 ID: `@143ndfws`** (聯成電腦總公司)")
            
            # 顯示目前綁定狀態
            token = sys.get_user_token(email)
            if token:
                st.success(f"✅ 已綁定 LINE (ID: {token[:4]}****{token[-4:]})")
            else:
                st.warning("❌ 尚未綁定，請掃描 QR Code 或搜尋 ID 加好友。")

def render_personal_task_module(user):
    if 'batch_df' not in st.session_state:
        st.session_state.batch_df = pd.DataFrame({
            "task_name": [""] * 10, "description": [""] * 10,
            "start_date": [date.today()] * 10, "end_date": [date.today() + timedelta(days=7)] * 10,
            "size": ["M"] * 10
        })
    if 'editor_key' not in st.session_state: st.session_state.editor_key = 0

    def reset_editor():
        st.session_state.batch_df = pd.DataFrame({
            "task_name": [""] * 10, "description": [""] * 10,
            "start_date": [date.today()] * 10, "end_date": [date.today() + timedelta(days=7)] * 10,
            "size": ["M"] * 10
        })
        st.session_state.editor_key += 1

    t1, t2, t3 = st.tabs(["📝 我的任務清單", "➕ 批次新增任務", "📖 相關辦法"])

    with t1:
        st.subheader("我的任務清單")
        df_tasks = sys.get_df("tasks")
        if df_tasks.empty:
            st.info("尚無任何任務")
        else:
            df_tasks['owner_email'] = df_tasks['owner_email'].astype(str).str.strip().str.lower()
            my_email = str(user['email']).strip().lower()
            my_tasks = df_tasks[df_tasks['owner_email'] == my_email].copy()
            
            drafts = my_tasks[my_tasks['status'] == 'Draft']
            submitted = my_tasks[my_tasks['status'] == 'Submitted']
            approved = my_tasks[my_tasks['status'] == 'Approved']
            rejected = my_tasks[my_tasks['status'] == 'Rejected']

            st.markdown("### 💾 暫存任務")
            if not drafts.empty:
                st.dataframe(drafts[['task_name', 'start_date', 'end_date', 'size', 'description']], hide_index=True)
                draft_opts = [f"{r['task_name']} ({r['task_id']})" for i, r in drafts.iterrows()]
                selected_drafts = st.multiselect("勾選任務進行操作", draft_opts)
                
                c1, c2, c3 = st.columns(3)
                if c1.button("🚀 送出審核 (選取項目)"):
                    updates = []
                    for item in selected_drafts:
                        tid = item.split("(")[-1].replace(")", "")
                        updates.append({'task_id': tid, 'status': "Submitted"})
                    if updates:
                        sys.batch_update_tasks_status(updates)
                        st.success("已送出審核"); time.sleep(1); st.rerun()
                
                if c2.button("✏️ 帶入批次編輯 (並刪除原暫存)"):
                    load_data = []
                    ids_to_del = []
                    for item in selected_drafts:
                        tid = item.split("(")[-1].replace(")", "")
                        task_row = drafts[drafts['task_id'].astype(str) == str(tid)].iloc[0]
                        load_data.append({
                            "task_name": task_row['task_name'],
                            "description": task_row['description'],
                            "start_date": pd.to_datetime(task_row['start_date']).date(),
                            "end_date": pd.to_datetime(task_row['end_date']).date(),
                            "size": task_row['size']
                        })
                        ids_to_del.append(tid)
                    if load_data:
                        while len(load_data) < 10: load_data.append({"task_name": "", "description": "", "start_date": date.today(), "end_date": date.today()+timedelta(days=7), "size": "M"})
                        st.session_state.batch_df = pd.DataFrame(load_data)
                        st.session_state.editor_key += 1
                        sys.delete_batch_tasks_by_ids(ids_to_del)
                        st.success("已載入並刪除舊資料，請切換至「批次新增任務」頁籤"); time.sleep(2); st.rerun()
                if c3.button("🗑️ 刪除 (選取項目)"):
                    ids = [item.split("(")[-1].replace(")", "") for item in selected_drafts]
                    sys.delete_batch_tasks_by_ids(ids)
                    st.success("已刪除"); time.sleep(1); st.rerun()
            else: st.caption("無暫存任務")
            
            st.divider(); st.markdown("### ⏳ 送審中")
            if not submitted.empty: st.dataframe(submitted[['task_name', 'start_date', 'end_date', 'size', 'description']], hide_index=True)
            else: st.caption("無送審任務")
            
            st.divider(); st.markdown("### ✅ 已核可 / ⚠️ 被退回")
            if not rejected.empty:
                for i, r in rejected.iterrows():
                    with st.expander(f"⚠️ {r['task_name']} (被退回)"):
                        st.error(f"主管評語: {r['manager_comment']}")
                        with st.form(f"edit_rej_{r['task_id']}"):
                            st.write("修改後重新送出：")
                            nn = st.text_input("名稱", value=r['task_name']); nd = st.text_input("說明", value=r['description'])
                            c1, c2, c3 = st.columns(3)
                            ns = c1.date_input("開始", value=pd.to_datetime(r['start_date'])); ne = c2.date_input("結束", value=pd.to_datetime(r['end_date']))
                            nz = c3.selectbox("大小", ["S","M","L","XL"], index=["S","M","L","XL"].index(r['size']))
                            c_sub, c_del = st.columns(2)
                            if c_sub.form_submit_button("🚀 重送"):
                                sys.update_task_content(r['task_id'], nn, nd, ns, ne, nz, "Submitted")
                                st.success("已重送"); time.sleep(1); st.rerun()
                            if c_del.form_submit_button("🗑️ 刪除"):
                                sys.delete_task(r['task_id']); st.rerun()
            if not approved.empty:
                for i, r in approved.iterrows():
                    with st.expander(f"✅ {r['task_name']} ({r['points']}點)"):
                        st.write(f"📅 {r['start_date']} ~ {r['end_date']}")
                        exp = calc_expected_progress(r['start_date'], r['end_date'])
                        c1, c2 = st.columns(2)
                        c1.metric("目前進度", f"{r['progress_pct']}%"); c2.metric("預計進度", f"{exp}%", delta=r['progress_pct']-exp)
                        with st.form(f"p_{r['task_id']}"):
                            np = st.slider("更新進度", 0, 100, int(r['progress_pct'])); nd = st.text_input("回報說明", max_chars=50)
                            if st.form_submit_button("回報"):
                                sys.update_progress(r['task_id'], np, nd); st.rerun()

    with t2:
        st.subheader("批次新增任務")
        edited_tasks = st.data_editor(
            st.session_state.batch_df,
            column_config={
                "task_name": "任務名稱",
                "description": st.column_config.TextColumn("說明 (50字內)", max_chars=50),
                "start_date": st.column_config.DateColumn("開始日"),
                "end_date": st.column_config.DateColumn("結束日"),
                "size": st.column_config.SelectboxColumn("預估大小", options=["S", "M", "L", "XL"])
            },
            num_rows="dynamic", use_container_width=True, key=f"task_editor_{st.session_state.editor_key}"
        )
        c1, c2 = st.columns(2)
        if c1.button("💾 儲存為暫存 (Draft)", type="secondary"):
            valid_tasks = edited_tasks[edited_tasks['task_name'] != ""]
            if not valid_tasks.empty:
                valid_tasks['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(valid_tasks, initial_status="Draft")
                if succ: st.success(msg); reset_editor(); time.sleep(1); st.rerun()
                else: st.error(msg)
            else: st.warning("請填寫任務")
        if c2.button("🚀 送出審核 (Submit)", type="primary"):
            valid_tasks = edited_tasks[edited_tasks['task_name'] != ""]
            if not valid_tasks.empty:
                valid_tasks['owner_email'] = user['email']
                succ, msg = sys.batch_add_tasks(valid_tasks, initial_status="Submitted")
                if succ: st.success(msg); reset_editor(); time.sleep(1); st.rerun()
                else: st.error(msg)
            else: st.warning("請填寫任務")
        st.divider()
        with st.expander("📂 Excel 匯入任務"):
            sample_task = pd.DataFrame([{"任務名稱": "專案A", "說明": "開發", "開始日期": "2025-01-01", "結束日期": "2025-01-31", "大小": "M"}])
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

    with t3:
        st.subheader("📖 員工 KPI 考核辦法")
        st.markdown("1. 點數：S(1-3), M(4-6), L(7-9), XL(10-12)\n2. 預計進度：依天數計算\n3. 簽核：暫存 -> 送審 -> 核准/退件")

# --- UI Pages (Admin) ---
def admin_page():
    st.header("🔧 管理後台")
    change_password_ui("admin", "admin")
    tab1, tab2, tab3 = st.tabs(["👥 員工管理", "🏢 組織圖", "⚙️ 系統設定"])
    
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
            cols_order = ['刪除', 'email', 'name', 'password', 'department', 'manager_email', 'role', 'line_token']
            edited_df = st.data_editor(df_emp[cols_order], column_config={"刪除": st.column_config.CheckboxColumn(default=False), "email": st.column_config.TextColumn(disabled=True)}, use_container_width=True, hide_index=True)
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
            edited_dept = st.data_editor(df_dept[cols_order], column_config={"刪除": st.column_config.CheckboxColumn(default=False), "dept_id": st.column_config.TextColumn(disabled=True)}, use_container_width=True, hide_index=True)
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

    with tab3:
        st.subheader("⚙️ 系統設定")
        st.write("設定公司 Logo (圖片)")
        
        current_logo = sys.get_setting("logo")
        if current_logo:
            st.image(current_logo, width=200, caption="目前 Logo")
        
        up_logo = st.file_uploader("上傳新 Logo (建議 < 50KB)", type=["png", "jpg", "jpeg"])
        if up_logo:
            if st.button("上傳並儲存"):
                try:
                    bytes_data = up_logo.getvalue()
                    base64_str = base64.b64encode(bytes_data).decode()
                    full_str = f"data:image/png;base64,{base64_str}"
                    if len(full_str) > 50000:
                        st.error("圖片過大 (超過 50,000 字元)，請壓縮後再試，或使用 URL 方式。")
                    else:
                        sys.update_setting("logo", full_str)
                        st.success("Logo 已更新！"); time.sleep(1); st.rerun()
                except Exception as e:
                    st.error(f"處理失敗: {e}")
        
        st.divider()
        st.write("或輸入 Logo 圖片網址 (URL)")
        logo_url = st.text_input("圖片連結", placeholder="https://example.com/logo.png")
        if st.button("儲存 URL"):
            if logo_url:
                sys.update_setting("logo", logo_url)
                st.success("Logo URL 已更新"); time.sleep(1); st.rerun()

# --- 6. 登入頁 ---
def login_page():
    st.markdown("## 📈 聯成教育員工KPI考核系統")
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

# --- 7. 員工頁面入口 (關鍵補回) ---
def employee_page():
    user = st.session_state.user
    st.header(f"👋 {user['name']}")
    change_password_ui("user", user['email'])
    render_personal_task_module(user)

# --- Entry ---
if 'user' not in st.session_state: st.session_state.user = None

logo_data = sys.get_setting("logo")
with st.sidebar:
    if logo_data:
        try:
            if logo_data.startswith("http"): st.image(logo_data, use_column_width=True)
            else:
                if not logo_data.startswith("data:image"): logo_data = f"data:image/png;base64,{logo_data}"
                st.image(logo_data, use_column_width=True)
        except: pass
    st.divider()

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
        else: 
            employee_page()
