# -*- coding: utf-8 -*-
import json
import os
import sys
import socket
import struct
import hashlib
import binascii

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.clock import Clock
from kivy.utils import get_color_from_hex

# Application Title
APP_TITLE = "MikroTik Voucher Manager"

# Setup local storage data file path for Android / Mobile environment
# In Android, standard system paths are restricted, so we use internal application path
try:
    from android.storage import app_storage_path
    _BASE_DIR = app_storage_path()
except ImportError:
    if getattr(sys, "frozen", False):
        _BASE_DIR = os.path.dirname(sys.executable)
    else:
        _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FILE = os.path.join(_BASE_DIR, "routers.json")


# ----------------------------------------------------------------------
# RouterOS API client (pure python implementation of the binary API)
# ----------------------------------------------------------------------
class RouterOSError(Exception):
    pass


class RouterOSAPI:
    def __init__(self, host, port=8728, timeout=6):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _write_len(self, length):
        if length < 0x80:
            self.sock.sendall(bytes([length]))
        elif length < 0x4000:
            length |= 0x8000
            self.sock.sendall(struct.pack(">H", length))
        elif length < 0x200000:
            length |= 0xC00000
            self.sock.sendall(struct.pack(">I", length)[1:])
        elif length < 0x10000000:
            length |= 0xE0000000
            self.sock.sendall(struct.pack(">I", length))
        else:
            self.sock.sendall(bytes([0xF0]))
            self.sock.sendall(struct.pack(">I", length))

    def _write_word(self, word):
        data = word.encode("utf-8")
        self._write_len(len(data))
        self.sock.sendall(data)

    def _write_sentence(self, words):
        for w in words:
            self._write_word(w)
        self._write_word("")

    def _read_bytes(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterOSError("Connection closed by router")
            buf += chunk
        return buf

    def _read_len(self):
        c = self._read_bytes(1)[0]
        if c & 0x80 == 0x00:
            return c
        elif c & 0xC0 == 0x80:
            b = self._read_bytes(1)
            return ((c & ~0xC0) << 8) + b[0]
        elif c & 0xE0 == 0xC0:
            b = self._read_bytes(2)
            return ((c & ~0xE0) << 16) + (b[0] << 8) + b[1]
        elif c & 0xF0 == 0xE0:
            b = self._read_bytes(3)
            return ((c & ~0xF0) << 24) + (b[0] << 16) + (b[1] << 8) + b[2]
        elif c == 0xF0:
            b = self._read_bytes(4)
            return (b[0] << 24) + (b[1] << 16) + (b[2] << 8) + b[3]
        raise RouterOSError("Bad length encoding")

    def _read_word(self):
        length = self._read_len()
        if length == 0:
            return ""
        return self._read_bytes(length).decode("utf-8", errors="replace")

    def _read_sentence(self):
        words = []
        while True:
            w = self._read_word()
            if w == "":
                break
            words.append(w)
        return words

    def talk(self, words):
        self._write_sentence(words)
        output = []
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            reply = sentence[0]
            attrs = {}
            for w in sentence[1:]:
                if w.startswith("="):
                    body = w[1:]
                    if "=" in body:
                        k, v = body.split("=", 1)
                    else:
                        k, v = body, ""
                    attrs[k] = v
            output.append((reply, attrs))
            if reply == "!done":
                break
            if reply == "!trap" or reply == "!fatal":
                if reply == "!fatal":
                    break
        return output

    def login(self, username, password):
        res = self.talk(["/login", "=name=" + username, "=password=" + password])
        for reply, attrs in res:
            if reply == "!trap":
                raise RouterOSError(attrs.get("message", "Login failed"))
            if reply == "!done" and "ret" in attrs:
                challenge = attrs["ret"]
                md5 = hashlib.md5()
                md5.update(b"\x00")
                md5.update(password.encode("utf-8"))
                md5.update(binascii.unhexlify(challenge))
                res2 = self.talk([
                    "/login",
                    "=name=" + username,
                    "=response=00" + md5.hexdigest(),
                ])
                for reply2, attrs2 in res2:
                    if reply2 == "!trap":
                        raise RouterOSError(attrs2.get("message", "Login failed"))
                return True
        return True


def api_connect(router):
    api = RouterOSAPI(router["ip"], router.get("port", 8728))
    api.connect()
    api.login(router["user"], router["password"])
    return api


def check_trap(res, action_desc):
    for reply, attrs in res:
        if reply == "!trap":
            raise RouterOSError(f"{action_desc} failed: {attrs.get('message', 'not allowed / permission denied')}")
    return res


def load_routers():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_routers(routers):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(routers, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# Helper global functions for Alerts
def show_popup_msg(title, text):
    content = BoxLayout(orientation='vertical', padding=10, spacing=10)
    content.add_widget(Label(text=text, halign="center", valign="middle"))
    close_btn = Button(text="OK", size_hint_y=None, height=45)
    content.add_widget(close_btn)
    popup = Popup(title=title, content=content, size_hint=(0.8, 0.4))
    close_btn.bind(on_release=popup.dismiss)
    popup.open()


# ----------------------------------------------------------------------
# KIVY UI SCREENS
# ----------------------------------------------------------------------
class MainMenu(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=40, spacing=20)
        
        # Dark Theme Background Color Trick
        self.bg_color = get_color_from_hex("#0d1117")
        
        title = Label(text=APP_TITLE, font_size='24sp', bold=True, color=(1, 1, 1, 1), size_hint_y=None, height=100)
        layout.add_widget(title)
        
        btn_manage = Button(text="Manage Router", font_size='18sp', background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=60)
        btn_manage.bind(on_release=lambda x: setattr(self.manager, 'current', 'manage_router_menu'))
        layout.add_widget(btn_manage)
        
        btn_choose = Button(text="Choose Router", font_size='18sp', background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=60)
        btn_choose.bind(on_release=lambda x: self.go_to_choose())
        layout.add_widget(btn_choose)
        
        layout.add_widget(BoxLayout()) # Spacer
        self.add_widget(layout)

    def go_to_choose(self):
        self.manager.get_screen('choose_router').refresh_list()
        self.manager.current = 'choose_router'


class ManageRouterMenu(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=40, spacing=20)
        
        title = Label(text="Manage Router", font_size='22sp', bold=True, size_hint_y=None, height=80)
        layout.add_widget(title)
        
        btn_add = Button(text="Add Router", font_size='18sp', background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=60)
        btn_add.bind(on_release=lambda x: self.go_to_add())
        layout.add_widget(btn_add)
        
        btn_list = Button(text="Router List", font_size='18sp', background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=60)
        btn_list.bind(on_release=lambda x: self.go_to_list())
        layout.add_widget(btn_list)
        
        btn_back = Button(text="<- Back", font_size='16sp', size_hint_y=None, height=50)
        btn_back.bind(on_release=lambda x: setattr(self.manager, 'current', 'main_menu'))
        layout.add_widget(btn_back)
        
        layout.add_widget(BoxLayout()) # Spacer
        self.add_widget(layout)

    def go_to_add(self):
        self.manager.get_screen('add_router_form').setup_form(None)
        self.manager.current = 'add_router_form'

    def go_to_list(self):
        self.manager.get_screen('router_list').refresh_routers_view()
        self.manager.current = 'router_list'


class AddRouterForm(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.editing_index = None
        
        self.layout = BoxLayout(orientation='vertical', padding=20, spacing=10)
        
        self.title_lbl = Label(text="Add Router", font_size='20sp', bold=True, size_hint_y=None, height=50)
        self.layout.add_widget(self.title_lbl)
        
        # Form grid
        grid = GridLayout(cols=2, spacing=10, size_hint_y=None)
        grid.bind(minimum_height=grid.setter('height'))
        
        self.inputs = {}
        fields = [("name", "Router Name"), ("ip", "IP Address"),
                  ("port", "API Port (8728)"), ("user", "Username"), ("password", "Password")]
        
        for key, label_text in fields:
            lbl = Label(text=label_text, size_hint_x=0.4, halign='left', size_hint_y=None, height=45)
            txt_input = TextInput(multiline=False, password=(key == "password"), size_hint_y=None, height=45)
            grid.add_widget(lbl)
            grid.add_widget(txt_input)
            self.inputs[key] = txt_input
            
        self.layout.add_widget(grid)
        
        btn_save = Button(text="Save Router", background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=55)
        btn_save.bind(on_release=lambda x: self.save_data())
        self.layout.add_widget(btn_save)
        
        btn_back = Button(text="<- Cancel", size_hint_y=None, height=50)
        btn_back.bind(on_release=lambda x: setattr(self.manager, 'current', 'manage_router_menu'))
        self.layout.add_widget(btn_back)
        
        self.layout.add_widget(BoxLayout()) # Spacer
        self.add_widget(self.layout)

    def setup_form(self, edit_index=None):
        self.editing_index = edit_index
        for inp in self.inputs.values():
            inp.text = ""
            
        app = App.get_running_app()
        if edit_index is not None:
            self.title_lbl.text = "Edit Router"
            r = app.routers[edit_index]
            self.inputs["name"].text = str(r.get("name", ""))
            self.inputs["ip"].text = str(r.get("ip", ""))
            self.inputs["port"].text = str(r.get("port", "8728"))
            self.inputs["user"].text = str(r.get("user", ""))
            self.inputs["password"].text = str(r.get("password", ""))
        else:
            self.title_lbl.text = "Add Router"
            self.inputs["port"].text = "8728"

    def save_data(self):
        name = self.inputs["name"].text.strip()
        ip = self.inputs["ip"].text.strip()
        port = self.inputs["port"].text.strip() or "8728"
        user = self.inputs["user"].text.strip()
        password = self.inputs["password"].text
        
        if not name or not ip or not user:
            show_popup_msg("Error", "Name, IP and Username are required.")
            return
        try:
            port = int(port)
        except ValueError:
            show_popup_msg("Error", "Port must be a valid number.")
            return
            
        entry = {"name": name, "ip": ip, "port": port, "user": user, "password": password}
        app = App.get_running_app()
        
        if self.editing_index is not None:
            app.routers[self.editing_index] = entry
        else:
            app.routers.append(entry)
            
        save_routers(app.routers)
        show_popup_msg("Saved", f"Router '{name}' saved successfully.")
        self.manager.get_screen('router_list').refresh_routers_view()
        self.manager.current = 'router_list'


class RouterList(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=20, spacing=10)
        
        title = Label(text="Router List", font_size='20sp', bold=True, size_hint_y=None, height=50)
        self.layout.add_widget(title)
        
        self.scroll = ScrollView()
        self.list_container = BoxLayout(orientation='vertical', spacing=10, size_hint_y=None)
        self.list_container.bind(minimum_height=self.list_container.setter('height'))
        self.scroll.add_widget(self.list_container)
        
        self.layout.add_widget(self.scroll)
        
        btn_back = Button(text="<- Back", size_hint_y=None, height=50)
        btn_back.bind(on_release=lambda x: setattr(self.manager, 'current', 'manage_router_menu'))
        self.layout.add_widget(btn_back)
        
        self.add_widget(self.layout)

    def refresh_routers_view(self):
        self.list_container.clear_widgets()
        app = App.get_running_app()
        app.routers = load_routers()
        
        if not app.routers:
            self.list_container.add_widget(Label(text="No routers saved yet.", size_hint_y=None, height=50))
            return
            
        for i, r in enumerate(app.routers):
            row = BoxLayout(orientation='horizontal', spacing=10, size_hint_y=None, height=60)
            
            lbl = Label(text=f"{r['name']}\n({r['ip']}:{r.get('port', 8728)})", font_size='14sp', halign='left')
            row.add_widget(lbl)
            
            btn_edit = Button(text="Edit", size_hint_x=0.2)
            btn_edit.bind(on_release=lambda x, idx=i: self.edit_router(idx))
            row.add_widget(btn_edit)
            
            btn_del = Button(text="Remove", size_hint_x=0.2, background_color=get_color_from_hex("#f85149"), background_normal='')
            btn_del.bind(on_release=lambda x, idx=i: self.confirm_delete(idx))
            row.add_widget(btn_del)
            
            self.list_container.add_widget(row)

    def edit_router(self, idx):
        self.manager.get_screen('add_router_form').setup_form(idx)
        self.manager.current = 'add_router_form'

    def confirm_delete(self, idx):
        app = App.get_running_app()
        r = app.routers[idx]
        
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(text=f"Remove router '{r['name']}'?"))
        
        btns = BoxLayout(orientation='horizontal', spacing=10, size_hint_y=None, height=45)
        yes_btn = Button(text="Yes")
        no_btn = Button(text="No")
        btns.add_widget(yes_btn)
        btns.add_widget(no_btn)
        content.add_widget(btns)
        
        popup = Popup(title="Confirm", content=content, size_hint=(0.8, 0.4))
        no_btn.bind(on_release=popup.dismiss)
        
        def do_delete(x):
            app.routers.pop(idx)
            save_routers(app.routers)
            popup.dismiss()
            self.refresh_routers_view()
            
        yes_btn.bind(on_release=do_delete)
        popup.open()


class ChooseRouter(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=40, spacing=20)
        
        title = Label(text="Choose Router", font_size='22sp', bold=True, size_hint_y=None, height=60)
        self.layout.add_widget(title)
        
        self.spinner = Spinner(text='Select a Router', values=(), size_hint_y=None, height=55)
        self.layout.add_widget(self.spinner)
        
        btn_continue = Button(text="Continue", font_size='18sp', background_color=get_color_from_hex("#1f6feb"), background_normal='', size_hint_y=None, height=60)
        btn_continue.bind(on_release=lambda x: self.go_forward())
        self.layout.add_widget(btn_continue)
        
        btn_back = Button(text="<- Back", size_hint_y=None, height=50)
        btn_back.bind(on_release=lambda x: setattr(self.manager, 'current', 'main_menu'))
        self.layout.add_widget(btn_back)
        
        self.layout.add_widget(BoxLayout()) # Spacer
        self.add_widget(self.layout)

    def refresh_list(self):
        app = App.get_running_app()
        app.routers = load_routers()
        names = [f"{r['name']} ({r['ip']})" for r in app.routers]
        self.spinner.values = names
        if names:
            self.spinner.text = names[0]
        else:
            self.spinner.text = 'Select a Router'

    def go_forward(self):
        app = App.get_running_app()
        if not app.routers:
            show_popup_msg("Warning", "Please add a router first.")
            return
        try:
            idx = self.spinner.values.index(self.spinner.text)
            app.selected_router = app.routers[idx]
            self.manager.get_screen('voucher_action').update_title()
            self.manager.current = 'voucher_action'
        except ValueError:
            show_popup_msg("Select", "Please select a valid router.")


class VoucherAction(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=15, spacing=10)
        
        self.title_lbl = Label(text="Router: None", font_size='16sp', bold=True, size_hint_y=None, height=40)
        self.layout.add_widget(self.title_lbl)
        
        self.layout.add_widget(Label(text="Voucher / Username", size_hint_y=None, height=25))
        self.voucher_entry = TextInput(multiline=False, font_size='16sp', size_hint_y=None, height=45)
        self.layout.add_widget(self.voucher_entry)
        
        btn_row = BoxLayout(orientation='horizontal', spacing=10, size_hint_y=None, height=55)
        btn_check = Button(text="Check Voucher", background_color=get_color_from_hex("#1f6feb"), background_normal='')
        btn_check.bind(on_release=lambda x: self.check_voucher())
        btn_reset = Button(text="Reset Voucher", background_color=get_color_from_hex("#1f6feb"), background_normal='')
        btn_reset.bind(on_release=lambda x: self.confirm_reset())
        
        btn_row.add_widget(btn_check)
        btn_row.add_widget(btn_reset)
        self.layout.add_widget(btn_row)
        
        self.result_box = TextInput(readonly=True, font_name='Roboto', font_size='12sp', background_color=get_color_from_hex("#161b22"), foreground_color=get_color_from_hex("#c9d1d9"))
        self.layout.add_widget(self.result_box)
        
        btn_back = Button(text="<- Back", size_hint_y=None, height=50)
        btn_back.bind(on_release=lambda x: setattr(self.manager, 'current', 'choose_router'))
        self.layout.add_widget(btn_back)
        
        self.add_widget(self.layout)

    def update_title(self):
        app = App.get_running_app()
        r = app.selected_router
        if r:
            self.title_lbl.text = f"Router: {r['name']} ({r['ip']})"
        else:
            self.title_lbl.text = "Router: None"
        self.voucher_entry.text = ""
        self.result_box.text = ""

    def log_text(self, text):
        self.result_box.text += text + "\n"

    def check_voucher(self):
        voucher = self.voucher_entry.text.strip()
        if not voucher:
            show_popup_msg("Input needed", "Enter a voucher/username first.")
            return
            
        self.result_box.text = ""
        app = App.get_running_app()
        r = app.selected_router
        
        self.log_text(f"Connecting to {r['ip']} ...")
        
        # Safe async execution simulation isn't mandatory, running directly
        api = None
        try:
            api = api_connect(r)
            res = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            found = [attrs for reply, attrs in res if reply == "!re"]
            if not found:
                self.log_text(f"Voucher '{voucher}' not found on this router.")
                return
            for attrs in found:
                self.log_text("---- Voucher Details ----")
                self.log_text(f"Name        : {attrs.get('name', '')}")
                self.log_text(f"Profile     : {attrs.get('profile', '')}")
                self.log_text(f"MAC Address : {attrs.get('mac-address', '(not bound)')}")
                self.log_text(f"Limit Uptime: {attrs.get('limit-uptime', 'unlimited')}")
                self.log_text(f"Uptime Used : {attrs.get('uptime', '0s')}")
                self.log_text(f"Bytes In    : {attrs.get('bytes-in', '0')}")
                self.log_text(f"Bytes Out   : {attrs.get('bytes-out', '0')}")
                self.log_text(f"Disabled    : {attrs.get('disabled', 'false')}")
        except Exception as e:
            self.log_text(f"ERROR: {e}")
        finally:
            if api:
                api.close()

    def confirm_reset(self):
        voucher = self.voucher_entry.text.strip()
        if not voucher:
            show_popup_msg("Input needed", "Enter a voucher/username first.")
            return
            
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(text=f"Reset voucher '{voucher}'?\nThis clears its MAC lock and disconnects device.", halign='center'))
        
        btns = BoxLayout(orientation='horizontal', spacing=10, size_hint_y=None, height=45)
        yes_btn = Button(text="Yes")
        no_btn = Button(text="No")
        btns.add_widget(yes_btn)
        btns.add_widget(no_btn)
        content.add_widget(btns)
        
        popup = Popup(title="Confirm Reset", content=content, size_hint=(0.8, 0.4))
        no_btn.bind(on_release=popup.dismiss)
        
        def run_reset(x):
            popup.dismiss()
            self.reset_voucher(voucher)
            
        yes_btn.bind(on_release=run_reset)
        popup.open()

    def reset_voucher(self, voucher):
        self.result_box.text = ""
        app = App.get_running_app()
        r = app.selected_router
        api = None
        temp_id = None
        try:
            self.log_text(f"Connecting to {r['ip']} ...")
            api = api_connect(r)
            res = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            found = [attrs for reply, attrs in res if reply == "!re"]
            if not found:
                self.log_text(f"Voucher '{voucher}' not found on this router.")
                return
            old = found[0]
            user_id = old.get(".id")

            active = api.talk(["/ip/hotspot/active/print", "?user=" + voucher])
            for reply, attrs in active:
                if reply == "!re" and ".id" in attrs:
                    res_k = api.talk(["/ip/hotspot/active/remove", "=.id=" + attrs[".id"]])
                    check_trap(res_k, "Disconnecting active session")
                    self.log_text("Active session disconnected.")

            temp_name = voucher + "__reset_tmp"
            res_rn = api.talk(["/ip/hotspot/user/set", "=.id=" + user_id, "=name=" + temp_name])
            check_trap(res_rn, "Preparing voucher for reset")
            temp_id = user_id

            exclude = {".id", "uptime", "bytes-in", "bytes-out", "packets-in",
                       "packets-out", "mac-address", "radius", "dynamic",
                       "invalid", ".nextid", "name"}
            new_params = ["=name=" + voucher] + [
                "=" + k + "=" + v for k, v in old.items()
                if k not in exclude and v not in ("", None)
            ]

            res_add = api.talk(["/ip/hotspot/user/add"] + new_params)
            check_trap(res_add, "Re-creating voucher without MAC lock")

            res_rm = api.talk(["/ip/hotspot/user/remove", "=.id=" + temp_id])
            check_trap(res_rm, "Removing old voucher copy")
            temp_id = None

            verify = api.talk(["/ip/hotspot/user/print", "?name=" + voucher])
            verify_attrs = next((a for rr, at in verify if rr == "!re" for a in [at]), {})
            new_mac = verify_attrs.get("mac-address", "")
            if new_mac:
                self.log_text(f"WARNING: MAC still shows as '{new_mac}'.")
            else:
                self.log_text(f"Confirmed: voucher '{voucher}' has no MAC lock — usable on any device.")
        except Exception as e:
            self.log_text(f"ERROR: {e}")
            if temp_id and api:
                try:
                    api.talk(["/ip/hotspot/user/set", "=.id=" + temp_id, "=name=" + voucher])
                    self.log_text("Rolled back: original voucher restored untouched.")
                except Exception:
                    self.log_text("Could not auto-restore original name — please check Winbox.")
        finally:
            if api:
                api.close()


# ----------------------------------------------------------------------
# MAIN APPLICATION KIVY ENTRYPOINT
# ----------------------------------------------------------------------
class MikroTikVoucherApp(App):
    def build(self):
        self.title = APP_TITLE
        self.routers = load_routers()
        self.selected_router = None
        
        sm = ScreenManager()
        sm.add_widget(MainMenu(name='main_menu'))
        sm.add_widget(ManageRouterMenu(name='manage_router_menu'))
        sm.add_widget(AddRouterForm(name='add_router_form'))
        sm.add_widget(RouterList(name='router_list'))
        sm.add_widget(ChooseRouter(name='choose_router'))
        sm.add_widget(VoucherAction(name='voucher_action'))
        
        return sm


if __name__ == "__main__":
    MikroTikVoucherApp().run()
