"""Windows GUI: offline image chat, complete saved exchanges and per-chat context control.

Only selected content is sent to Ollama. No network listeners are opened on Windows.
Run with .venv/Scripts/pythonw.exe -m local_ai_app.chat_gui (from installer directory).
"""
from __future__ import annotations

import calendar as pycalendar
import datetime as dt
import os
import json
import time
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import uuid

from .history_store import load_mounts, save_mounts
from .history_vault import HistoryVault
from .ollama_client import OllamaBridge
from .temporal import utc_now, iso_utc, last_user_gap
from .context_requests import parse_model_request, MARKER
from .memory_store import _atomic_json
from .local_calendar import LocalCalendar
from .local_calculator import calculate
from .audit_log import AuditTrail
from .tool_gateway import ToolPermissions, LocalToolGateway

ROOT = Path(__file__).resolve().parent.parent
IMAGE_TYPES = [('Images (PNG, JPEG, WebP)', '*.png *.jpg *.jpeg *.webp')]
REFERENCE_TYPES = [('Supported reference files', '*.txt *.md *.log *.csv *.json *.jsonl *.py *.rpy *.ps1 *.js *.ts *.xml *.html *.docx *.pdf *.png *.jpg *.jpeg *.webp'), ('All files', '*.*')]


class LocalChatApp:
    def __init__(self, window: tk.Tk):
        self.window = window
        self.window.title('Local AI Sandbox | Memory, Personas, Tools & Calendar')
        self.window.geometry('1350x850')
        self.window.minsize(840, 620)
        self.store = HistoryVault(ROOT / 'state' / 'history')
        self.bridge = OllamaBridge(ROOT)
        self.audit = AuditTrail(ROOT / 'state' / 'audit')
        self.calendar = LocalCalendar(ROOT / 'state' / 'calendar')
        self.tool_permissions = ToolPermissions(ROOT / 'state' / 'tools')
        self.calendar_month = dt.date.today().replace(day=1)
        self.calendar_selected = dt.date.today()
        self.mount_path = ROOT / 'state' / 'workspace_mounts.json'
        self.mounts = load_mounts(self.mount_path)
        self.queued_images: list[str] = []
        self.busy = False
        self.notifications: queue.Queue = queue.Queue()
        self._index = []
        self._rendering = False
        self.filter_text = tk.StringVar()
        self.search_mode = tk.StringVar(value='all')
        self.context_candidates = {}
        self._filter_after = None
        self.profile_var = tk.StringVar(value='balanced')
        self.persona_edit_id = 'balanced'
        self.suggest_while_sending = tk.BooleanVar(value=False)
        self.response_timing = tk.StringVar(value='No response timed yet')
        self.project_var = tk.StringVar(value='')
        self.memory_global = tk.BooleanVar(value=True)
        self.memory_project = tk.BooleanVar(value=True)
        self.memory_handoff = tk.BooleanVar(value=False)
        self.context_stats = tk.StringVar()
        self.status = tk.StringVar(value='Local-only history. Selected context is sent only when you press Send.')
        self.model_text = tk.StringVar(value='')
        self._build()
        self.refresh_everything()
        self.window.after(160, self.poll_worker)

    def _build(self):
        style = ttk.Style(self.window)
        try:
            style.theme_use('vista' if sys.platform == 'win32' else 'clam')
        except tk.TclError:
            pass
        style.configure('Treeview', rowheight=25)
        style.configure('Title.TLabel', font=('Segoe UI', 14, 'bold'))
        style.configure('Small.TLabel', font=('Segoe UI', 9))
        main = ttk.Frame(self.window, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text='Local AI | Conversation Library, Context & Memory', style='Title.TLabel').pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.model_text).pack(side=tk.RIGHT)
        selector = ttk.Frame(main)
        selector.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(selector, text='Conversation:').pack(side=tk.LEFT, padx=(0, 8))
        self.convo = ttk.Combobox(selector, state='readonly', width=56)
        self.convo.pack(side=tk.LEFT, padx=(0, 7))
        self.convo.bind('<<ComboboxSelected>>', self.change_conversation)
        ttk.Button(selector, text='New', command=self.new_conversation).pack(side=tk.LEFT, padx=3)
        ttk.Button(selector, text='Rename', command=self.rename_conversation).pack(side=tk.LEFT, padx=3)
        ttk.Button(selector, text='Delete conversation', command=self.delete_conversation).pack(side=tk.LEFT, padx=3)
        body = ttk.Frame(main)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_sidebar(body)
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_chat_tab()
        self._build_context_tab()
        self._build_requests_tab()
        self._build_profiles_tab()
        self._build_compare_tab()
        self._build_memory_tab()
        self._build_tools_tab()
        self._build_calendar_tab()
        self._build_audit_tab()
        self._build_import_tab()
        self._build_mount_tab()
        ttk.Label(main, textvariable=self.status, style='Small.TLabel').pack(fill=tk.X, pady=(9, 0))

    def _build_chat_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Chat')
        options = ttk.Frame(tab)
        options.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(options, text='Behavior profile:').pack(side=tk.LEFT)
        self.profile_combo = ttk.Combobox(options, textvariable=self.profile_var, values=['balanced','direct','creative'], state='readonly', width=24)
        self.profile_combo.pack(side=tk.LEFT, padx=(7, 12))
        self.profile_combo.bind('<<ComboboxSelected>>', self.change_profile)
        ttk.Label(options, text='Project memory scope:').pack(side=tk.LEFT)
        ttk.Entry(options, textvariable=self.project_var, width=25).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(options, text='Save project', command=self.save_project).pack(side=tk.LEFT)
        info = ttk.Label(tab, text='Managed Ollama chat: saves both sides. No filesystem tools. For code edits, use Launch Pi from the installer.', wraplength=900)
        info.pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(tab, textvariable=self.response_timing).pack(anchor=tk.W, pady=(0, 6))
        outer = ttk.Frame(tab)
        outer.pack(fill=tk.BOTH, expand=True)
        self.transcript = tk.Text(outer, wrap='word', state=tk.DISABLED, font=('Segoe UI', 10), padx=12, pady=10)
        self.transcript.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.transcript.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.transcript.configure(yscrollcommand=sb.set)
        self.transcript.tag_configure('role_user', foreground='#155A84', font=('Segoe UI', 10, 'bold'))
        self.transcript.tag_configure('role_assistant', foreground='#187342', font=('Segoe UI', 10, 'bold'))
        self.transcript.tag_configure('role_reference', foreground='#6C4B9C', font=('Segoe UI', 10, 'bold'))
        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(controls, text='Type a message (Ctrl+Enter to send):').pack(anchor=tk.W)
        self.prompt = tk.Text(controls, height=4, font=('Segoe UI', 10), wrap='word')
        self.prompt.pack(fill=tk.X, pady=(4, 6))
        self.prompt.bind('<Control-Return>', lambda event: (self.send(), 'break')[1])
        images = ttk.Frame(controls)
        images.pack(fill=tk.X)
        self.images_text = tk.StringVar(value='No image attached')
        ttk.Label(images, textvariable=self.images_text).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(images, text='Attach image(s)', command=self.attach_images).pack(side=tk.LEFT, padx=3)
        ttk.Button(images, text='Clear images', command=self.clear_images).pack(side=tk.LEFT, padx=3)
        self.send_btn = ttk.Button(images, text='Send to local model', command=self.send)
        self.send_btn.pack(side=tk.RIGHT, padx=(12, 0))

    def _build_context_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Context')
        ttk.Label(tab, text='Disk keyword search -> expand nearby/related messages -> preview -> admit only selected results. Search does not send the archive to Ollama.', wraplength=1000).pack(anchor=tk.W, pady=(0, 6))
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(top, text='Search words:').pack(side=tk.LEFT)
        filter_box = ttk.Entry(top, textvariable=self.filter_text)
        filter_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 12))
        self.filter_text.trace_add('write', lambda *_: self.schedule_context_search())
        self.search_mode_box = ttk.Combobox(top, textvariable=self.search_mode, values=['all','any','phrase'], width=8, state='readonly')
        self.search_mode_box.pack(side=tk.LEFT, padx=(0, 8))
        self.search_mode_box.bind('<<ComboboxSelected>>', lambda _: self.render_context_tree())
        ttk.Button(top, text='Search', command=self.render_context_tree).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(top, text='Rebuild Index', command=self.reindex_archive).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(top, textvariable=self.context_stats).pack(side=tk.RIGHT)
        frame = ttk.Frame(tab)
        frame.pack(fill=tk.BOTH, expand=True)
        cols = ('included', 'conversation', 'role', 'snippet', 'reason', 'date')
        self.context_tree = ttk.Treeview(frame, columns=cols, show='headings', height=13, selectmode='extended')
        definitions = [('included', 'Use?', 55), ('conversation', 'From', 165), ('role', 'Type', 95),
                       ('snippet', 'Preview', 390), ('reason', 'Source / ring', 135), ('date', 'Recorded', 135)]
        for key, title, width in definitions:
            self.context_tree.heading(key, text=title)
            self.context_tree.column(key, width=width, minwidth=50, stretch=(key == 'snippet'))
        self.context_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.context_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.context_tree.configure(yscrollcommand=sb.set)
        self.context_tree.bind('<Double-Button-1>', self.toggle_context)
        self.context_tree.bind('<<TreeviewSelect>>', self.preview_entry)
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X, pady=(8, 8))
        for label, callback in (
            ('Include highlighted', lambda: self.select_context_rows(True)),
            ('Exclude highlighted', lambda: self.select_context_rows(False)),
            ('Include visible', lambda: self.select_context_rows(True, all_visible=True)),
            ('Clear all allowed', self.clear_context),
            ('Delete highlighted entries...', self.delete_entries),
        ):
            ttk.Button(toolbar, text=label, command=callback).pack(side=tk.LEFT, padx=(0, 7))
        neighbors = ttk.Frame(tab)
        neighbors.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(neighbors, text='Expand related context:').pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(neighbors, text='+1 nearby', command=lambda: self.expand_context(1, False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(neighbors, text='+2 nearby', command=lambda: self.expand_context(2, False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(neighbors, text='+2 and keyword-related', command=lambda: self.expand_context(2, True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(neighbors, text='Save selection for Pi...', command=self.export_selected_for_pi).pack(side=tk.RIGHT, padx=3)
        ttk.Label(tab, text='Full selected entry (read-only preview):').pack(anchor=tk.W)
        preview = ttk.Frame(tab)
        preview.pack(fill=tk.BOTH)
        self.preview = tk.Text(preview, height=7, wrap='word', state=tk.DISABLED, font=('Consolas', 9))
        self.preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2 = ttk.Scrollbar(preview, orient=tk.VERTICAL, command=self.preview.yview)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview.configure(yscrollcommand=sb2.set)

    def _build_import_tab(self):
        tab = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(tab, text='Data')
        ttk.Label(tab, text='History stays on your computer', style='Title.TLabel').pack(anchor=tk.W, pady=(0, 12))
        ttk.Label(tab, text='The archive is in state/history/conversations/*.json; vault.json stores only folder/chat metadata; keyword-index.sqlite3 is a rebuildable on-disk search cache. Old v0.3 history.json remains as a migration source/backup until explicitly deleted. Terminal chats still require transcript import.', wraplength=950).pack(anchor=tk.W, pady=(0, 15))
        import_frame = ttk.LabelFrame(tab, text='Add selectable context or restore history', padding=12)
        import_frame.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(import_frame, text='Reference files become clearly labeled reference entries, not fake user/assistant messages. Import text, code, DOCX, optional PDF, or images.').pack(anchor=tk.W, pady=(0, 8))
        row = ttk.Frame(import_frame)
        row.pack(fill=tk.X)
        ttk.Button(row, text='Import reference file(s)', command=self.import_references).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(row, text='Import chat transcript / backup', command=self.import_history).pack(side=tk.LEFT)
        export = ttk.LabelFrame(tab, text='Export / Save history', padding=12)
        export.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(export, text='Portable ZIP backups contain full saved conversations, selected context flags and locally stored image attachments.').pack(anchor=tk.W, pady=(0, 8))
        row = ttk.Frame(export)
        row.pack(fill=tk.X)
        ttk.Button(row, text='Save current conversation...', command=lambda: self.export_history(False)).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(row, text='Save ALL history...', command=lambda: self.export_history(True)).pack(side=tk.LEFT)
        destructive = ttk.LabelFrame(tab, text='Delete saved history', padding=12)
        destructive.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(destructive, text='Deleting history also removes copied image attachments that are no longer used. Exported ZIP files elsewhere are not deleted.').pack(anchor=tk.W, pady=(0, 8))
        row = ttk.Frame(destructive)
        row.pack(fill=tk.X)
        ttk.Button(row, text='Delete this entire conversation...', command=self.delete_conversation).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(row, text='DELETE ALL SAVED HISTORY...', command=self.clear_all_history).pack(side=tk.LEFT)
        ttk.Button(row, text='Remove old v0.3 backup...', command=self.purge_legacy_backup).pack(side=tk.LEFT,padx=(12,0))
        ttk.Button(tab, text='Open local history folder', command=lambda: self.open_folder(ROOT / 'state' / 'history')).pack(anchor=tk.W, pady=(14, 0))

    def _build_mount_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Workspace')
        ttk.Label(tab, text='This controls extra folders available to the Pi coding agent, NOT the ordinary Ollama chatbot. The installer workspace/ folder remains available as /workspace.', wraplength=1000).pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(tab, text=f'Default writable folder: {ROOT / "workspace"}', wraplength=1000).pack(anchor=tk.W, pady=(0, 10))
        frame = ttk.Frame(tab)
        frame.pack(fill=tk.BOTH, expand=True)
        self.mount_tree = ttk.Treeview(frame, columns=('path', 'mode', 'inside'), show='headings', height=12)
        for field, title, width in [('path', 'Windows folder', 650), ('mode', 'Access', 120), ('inside', 'Container path', 260)]:
            self.mount_tree.heading(field, text=title)
            self.mount_tree.column(field, width=width, stretch=(field == 'path'))
        self.mount_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.mount_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.mount_tree.configure(yscrollcommand=sb.set)
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X, pady=(12, 4))
        ttk.Button(toolbar, text='Add folder (read-only default)', command=self.add_mount).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text='Switch read-only / read-write', command=self.toggle_mount).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text='Remove selected access', command=self.remove_mount).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text='Open default workspace', command=lambda: self.open_folder(ROOT / 'workspace')).pack(side=tk.RIGHT)
        ttk.Label(tab, text='Changes apply the next time you launch Pi. Read-write mounts allow Pi to modify/delete originals. No changes to Docker configuration until Pi is launched.', wraplength=1000).pack(anchor=tk.W, pady=(12, 0))

    def refresh_everything(self):
        self.refresh_conversations()
        self.render_sidebar()
        self.refresh_profile_choices()
        self.profile_var.set(self.store.active.get('profile', 'balanced'))
        self.project_var.set(self.store.active.get('project', ''))
        policy = self.store.active.get('memory_policy', {'global': True, 'project': True, 'handoff': False})
        self.memory_global.set(policy['global'])
        self.memory_project.set(policy['project'])
        self.memory_handoff.set(policy['handoff'])
        self.store.memory_options = dict(policy)
        self.render_transcript()
        self.render_context_tree()
        self.render_mounts()
        self.render_memories()
        self.render_tools()
        self.render_requests()
        self.render_calendar()
        self.render_audit()
        self.refresh_persona_editor()
        try:
            self.model_text.set('Local Ollama model: ' + self.bridge.model() + ' | Offline bridge required for Send')
        except Exception:
            self.model_text.set('Install sandbox before chatting')

    def refresh_conversations(self):
        self._index = [c['id'] for c in self.store.conversations]
        self.convo['values'] = [f"{c['name']}  ({c['item_count']} items)" for c in self.store.conversations]
        active_id = self.store.active['id']
        if active_id in self._index:
            self.convo.current(self._index.index(active_id))

    def change_conversation(self, _event=None):
        index = self.convo.current()
        if index >= 0 and index < len(self._index):
            self.store.set_active(self._index[index])
            self.context_candidates.clear()
            self.refresh_everything()

    def new_conversation(self):
        title = simpledialog.askstring('Create conversation', 'Conversation name:', initialvalue='New conversation', parent=self.window)
        if title is None:
            return
        selected = self.conversation_tree.selection()
        folder_id = selected[0][7:] if selected and selected[0].startswith('folder:') and selected[0] != 'folder:unfiled' else ''
        self.store.create_conversation(title.strip() or 'New conversation', folder_id)
        self.refresh_everything()

    def rename_conversation(self):
        c = self.store.active
        title = simpledialog.askstring('Rename conversation', 'New title:', initialvalue=c['name'], parent=self.window)
        if title is not None:
            self.store.rename(c['id'], title)
            self.refresh_everything()

    def delete_conversation(self):
        c = self.store.active
        if not messagebox.askyesno('Delete conversation?', f"Permanently delete '{c['name']}' and its {len(c['items'])} entries?\nThis removes references from other context selections.", parent=self.window, icon='warning'):
            return
        refs = {item['id'] for item in c['items']}
        dependent = self.store.memory.referenced_by(refs)
        if dependent and not messagebox.askyesno('Review derived memories',
                f'{len(dependent)} memories cite this conversation. Deleting the conversation will NOT delete those memories. Continue?', parent=self.window):
            return
        self.store.delete_conversation(c['id'])
        self.refresh_everything()

    def render_transcript(self):
        self.transcript.configure(state=tk.NORMAL)
        self.transcript.delete('1.0', tk.END)
        for item in self.store.active['items']:
            role = item['role']
            active_name = self.store.personas.get(self.store.active.get('profile', 'balanced')).get('agent_name') or 'Qwen'
            label = {'user': 'YOU', 'assistant': (item.get('timing') or {}).get('agent_name') or active_name, 'reference': 'IMPORTED REFERENCE'}[role]
            self.transcript.insert(tk.END, f'{label}  {item["timestamp"]}\n', f'role_{role}')
            if item.get('origin'):
                self.transcript.insert(tk.END, 'Source: ' + item['origin'] + '\n')
            self.transcript.insert(tk.END, item['content'] + '\n')
            timing=item.get('timing') or {}
            if role=='user' and timing.get('gap_since_previous_user_seconds') is not None:
                self.transcript.insert(tk.END, f"[Time since previous user message: {timing['gap_since_previous_user_seconds']} seconds]\n")
            if role=='assistant' and timing.get('tools_used'):
                self.transcript.insert(tk.END, '[Verified local tools: ' + ', '.join(timing['tools_used']) + ']\n')
            if role=='assistant' and timing.get('generation_seconds') is not None:
                self.transcript.insert(tk.END, f"[Local model response time: {timing['generation_seconds']:.2f} seconds]\n")
            if item.get('attachments'):
                self.transcript.insert(tk.END, f'[{len(item["attachments"])} attached image(s)]\n')
            self.transcript.insert(tk.END, '\n')
        self.transcript.see(tk.END)
        self.transcript.configure(state=tk.DISABLED)

    def schedule_context_search(self):
        if self._filter_after:
            self.window.after_cancel(self._filter_after)
        self._filter_after = self.window.after(280, self.render_context_tree)

    def render_context_tree(self):
        self._filter_after = None
        if not hasattr(self, 'context_tree'):
            return
        self.context_tree.delete(*self.context_tree.get_children())
        selected = set(self.store.active['selected_ids'])
        query = self.filter_text.get().strip()
        try:
            if query:
                rows = self.store.search(query, limit=120, mode=self.search_mode.get())
            else:
                active = self.store.active
                rows = [{'item_id': i['id'], 'conversation_id': active['id'],
                         'conversation_name': active['name'], 'role': i['role'],
                         'snippet': i['content'][:220], 'timestamp': i['timestamp'],
                         'reason': 'Current conversation'} for i in active['items'][-60:]]
                seen = {row['item_id'] for row in rows}
                for i in self.store.selected_preview(limit=200):
                    if i['id'] not in seen:
                        meta = self.store._meta(i['conversation_id'])
                        rows.append({'item_id': i['id'], 'conversation_id': i['conversation_id'],
                                     'conversation_name': meta['name'], 'role': i['role'],
                                     'snippet': i['snippet'], 'timestamp': i['timestamp'],
                                     'reason': 'Previously admitted'})
                        seen.add(i['id'])
        except Exception as exc:
            self.status.set('Search failed: ' + str(exc))
            return
        combined = {row['item_id']: row for row in rows}
        combined.update(self.context_candidates)
        for eid, item in combined.items():
            self.context_tree.insert('', tk.END, iid=eid,
                                     values=('YES' if eid in selected else 'no',
                                             item.get('conversation_name', '')[:36],
                                             item['role'], ' '.join(item['snippet'].split())[:180],
                                             item.get('reason', 'Search')[:34],
                                             item['timestamp'][:16].replace('T', ' ')))
        self.refresh_context_stats()
        self.status.set(f'Found {len(rows)} indexed/current matches. Search never sends archive text to Ollama.')

    def reindex_archive(self):
        try:
            self.store.rebuild_index()
            self.context_candidates.clear()
            self.render_context_tree()
            self.status.set('Rebuilt on-disk keyword index from authoritative conversation JSON files.')
        except Exception as exc:
            messagebox.showerror('Index rebuild failed',str(exc),parent=self.window)

    def purge_legacy_backup(self):
        source=self.store.legacy_file
        if not source.exists():
            messagebox.showinfo('No legacy backup','There is no v0.3 history.json to remove.',parent=self.window)
            return
        if messagebox.askyesno('Delete original v0.3 backup?',
                 'The original state/history/history.json contains the complete pre-migration conversation archive.\n'
                 'Only remove it AFTER verifying the new conversations and exporting a backup.\n\nDelete this legacy copy?',
                 icon='warning',parent=self.window):
            source.unlink()
            self.status.set('Removed legacy history.json backup. Active v0.4 conversations unchanged.')

    def refresh_context_stats(self):
        # No loading of other conversations for a counter.
        self.context_stats.set(f"Admitted: {self.store.selected_count()} item(s)")

    def _context_ids(self, all_visible=False):
        if all_visible:
            return list(self.context_tree.get_children())
        return list(self.context_tree.selection())

    def select_context_rows(self, allow: bool, all_visible=False):
        ids = self._context_ids(all_visible)
        if ids:
            self.store.set_selected(ids, allow)
            self.render_context_tree()

    def clear_context(self):
        ids = list(self.store.active['selected_ids'])
        if ids:
            self.store.set_selected(ids, False)
            self.render_context_tree()
        self.status.set('All context excluded for this conversation. Saved history unchanged.')

    def toggle_context(self, event):
        eid = self.context_tree.identify_row(event.y)
        if eid:
            self.store.set_selected([eid], eid not in self.store.active['selected_ids'])
            self.render_context_tree()

    def expand_context(self, radius, related):
        ids = self._context_ids()
        if not ids:
            messagebox.showinfo('Select a seed', 'Highlight a search result first.', parent=self.window)
            return
        for row in self.store.expand_nearby(ids, radius=radius, related=related):
            self.context_candidates[row['id']] = {
                'item_id': row['id'], 'conversation_id': row['conversation_id'],
                'conversation_name': row['conversation_name'], 'role': row['role'],
                'snippet': row['snippet'], 'timestamp': row['timestamp'],
                'reason': row['reason']}
        self.render_context_tree()
        self.status.set('Related candidates discovered. None are admitted until you include them.')

    def preview_entry(self, _event=None):
        ids = self.context_tree.selection()
        item = self.store.get_item(ids[0]) if ids else None
        content = ''
        if item:
            content = item['content']
            if item.get('attachments'):
                content += '\n\n' + ', '.join('Image: ' + name for name in item['attachments'])
        self.preview.configure(state=tk.NORMAL)
        self.preview.delete('1.0', tk.END)
        self.preview.insert(tk.END, content)
        self.preview.configure(state=tk.DISABLED)

    def delete_entries(self):
        ids = self._context_ids()
        if not ids:
            return
        dependent = self.store.memory.referenced_by(set(ids))
        extra = f'\n{len(dependent)} linked memory record(s) may still contain derived information and require separate review.' if dependent else ''
        if messagebox.askyesno('Delete entries permanently?',
                f'Delete {len(ids)} selected history item(s) from stored conversations?\n'
                f'The pre-v0.4 history.json and exported backups are separate copies.{extra}',
                parent=self.window, icon='warning'):
            removed = self.store.delete_items(ids)
            self.context_candidates.clear()
            self.refresh_everything()
            self.status.set(f'Deleted {removed} indexed history entries.')

    def export_selected_for_pi(self):
        target = ROOT / 'workspace' / 'approved_context.json'
        if target.exists() and not messagebox.askyesno('Replace selected context?',
                'Replace the existing approved_context.json in the writable Pi workspace?', parent=self.window):
            return
        try:
            n, m = self.store.export_selected_for_pi(target)
            self.status.set(f'Exported {n} admitted messages and {m} approved memories to {target}')
            messagebox.showinfo('Selected context saved',
                f'Pi can now inspect /workspace/approved_context.json\n{n} messages, {m} approved memories.\n'
                'No entire archive was mounted.', parent=self.window)
        except Exception as exc:
            messagebox.showerror('Context export failed', str(exc), parent=self.window)

    def attach_images(self):
        paths = filedialog.askopenfilenames(title='Attach images for the next message', filetypes=IMAGE_TYPES, parent=self.window)
        if paths:
            combined = list(dict.fromkeys(self.queued_images + list(paths)))
            if len(combined) > 12:
                messagebox.showerror('Too many images', 'Maximum 12 images per message.', parent=self.window)
                return
            self.queued_images = combined
            self.images_text.set(f"{len(combined)} image(s): " + ', '.join(Path(p).name for p in combined)[:100])

    def clear_images(self):
        self.queued_images.clear()
        self.images_text.set('No image attached')

    def send(self):
        if self.busy:
            return
        text = self.prompt.get('1.0', tk.END).strip()
        paths = self.queued_images[:]
        if not text and not paths:
            return
        conversation_id = self.store.active['id']
        profile = self.store.personas.get(self.store.active.get('profile', 'balanced'))
        if text and self.suggest_while_sending.get() and profile['retrieval'] == 'ask' and getattr(self, '_ignored_request', None) != (conversation_id, text):
            try:
                request = self.store.propose_context(text, cid=conversation_id)
                if request and not getattr(self, '_ignored_request', None) == (conversation_id, text):
                    if messagebox.askyesno('Possible related history',
                            f'{len(request["item_ids"])} previous entries might help. Review before sending? '
                            'No saved history is sent to Qwen unless you approve it.', parent=self.window):
                        self._ignored_request = (conversation_id, text)
                        self.tabs.select(self.request_tab)
                        self.render_requests()
                        return
                    self._ignored_request = (conversation_id, text)
            except Exception as exc:
                self.status.set('Context suggestion unavailable: ' + str(exc))
        sent_at = utc_now()
        agent_name = profile.get('agent_name', '').strip() or 'Qwen'
        message_timezone = self.store.personas.timezone
        user_gap = last_user_gap(self.store.active, sent_at)
        try:
            self.store.memory_options = {'global': self.memory_global.get(), 'project': self.memory_project.get(), 'handoff': self.memory_handoff.get()}
            messages = self.store.build_messages(text, paths, at=sent_at)
        except Exception as exc:
            messagebox.showerror('Context preparation failed', str(exc), parent=self.window)
            return
        try:
            model = self.bridge.model()
        except Exception as exc:
            messagebox.showerror('No configured model', str(exc), parent=self.window)
            return
        self.busy = True
        self.send_btn.configure(state=tk.DISABLED)
        self.audit.record('chat.send', conversation_id=conversation_id)
        self.status.set(f'Generating locally with {model}... Calendar proposals require confirmation.')
        def worker():
            started = time.monotonic()
            try:
                gateway = LocalToolGateway(self.calendar, self.audit, self.tool_permissions,
                                           timezone=message_timezone)
                result = gateway.answer(self.bridge, messages, conversation_id=conversation_id)
                elapsed = round(time.monotonic() - started, 3)
                self.notifications.put(('ok', conversation_id, text, paths, result,
                                        iso_utc(sent_at), iso_utc(), user_gap, elapsed, agent_name))
            except Exception as exc:
                self.notifications.put(('error', conversation_id, text, paths, str(exc),
                                        iso_utc(sent_at), iso_utc(), user_gap, None, agent_name))
        threading.Thread(target=worker, daemon=True).start()

    def poll_worker(self):
        try:
            while True:
                packet = self.notifications.get_nowait()
                kind = packet[0]
                if kind == 'venv':
                    _, _, _, _, data = packet
                    self.status.set('Pi Python .venv setup completed.' if data[0] else 'Pi Python setup failed.')
                    if data[0]:
                        messagebox.showinfo('Pi virtual environment ready', data[1][-2000:], parent=self.window)
                    else:
                        messagebox.showerror('Pi Python setup failed', data[1][-3000:], parent=self.window)
                    continue
                if kind == 'comparison':
                    _, result = packet
                    self.busy = False
                    self.send_btn.configure(state=tk.NORMAL)
                    self.finish_comparison(result)
                    continue
                _, conversation_id, user_text, paths, data, user_at, reply_at, gap, seconds, agent_name = packet
                self.busy = False
                self.send_btn.configure(state=tk.NORMAL)
                if kind == 'ok':
                    answer = data['text'] if isinstance(data, dict) else data
                    tools_used = [x['tool'] for x in data.get('tools', [])] if isinstance(data, dict) else []
                    self.audit.record('chat.complete', conversation_id=conversation_id)
                    try:
                        self.store.add_message('user', user_text, paths, conversation_id=conversation_id,
                                               timestamp=user_at, timing={'gap_since_previous_user_seconds': gap})
                        clean = MARKER.sub('', answer).strip() or '[Model requested previous context.]'
                        self.store.add_message('assistant', clean, conversation_id=conversation_id,
                                               timestamp=reply_at, timing={'generation_seconds': seconds,
                                               'agent_name': agent_name, 'tools_used': tools_used})
                        hint = parse_model_request(answer)
                        request = self.store.propose_context(hint, cid=conversation_id, from_model=True) if hint else None
                        if request:
                            self.status.set(f'Model requested {len(request["item_ids"])} relevant history entries; review in Context Requests.')
                        else:
                            self.status.set('Complete. Both sides saved; selected context is available for the next turn.')
                    except Exception as exc:
                        messagebox.showerror('History write error', f'Model replied, but history saving failed: {exc}', parent=self.window)
                    if self.store.active['id'] == conversation_id and self.prompt.get('1.0', tk.END).strip() == user_text:
                        self.prompt.delete('1.0', tk.END)
                        self.clear_images()
                    self.response_timing.set(f'Last inference: {seconds:.2f}s | Tools: {", ".join(tools_used) or "none"} | Gap since previous user message: ' +
                                             ('first message' if gap is None else f'{gap} seconds'))
                    self.refresh_everything()
                else:
                    self.audit.record('chat.error', status='error', conversation_id=conversation_id)
                    self.status.set('Local inference failed; input retained for retry.')
                    messagebox.showerror('Local chat failed', data, parent=self.window)
        except queue.Empty:
            pass
        self.window.after(160, self.poll_worker)

    def import_references(self):
        paths = filedialog.askopenfilenames(title='Import documents, source code, or images as selectable context', filetypes=REFERENCE_TYPES, parent=self.window)
        if not paths:
            return
        imported, errors = 0, []
        for p in paths:
            try:
                self.store.add_reference(Path(p))
                imported += 1
            except Exception as exc:
                errors.append(f'{Path(p).name}: {exc}')
        self.refresh_everything()
        self.status.set(f'Imported {imported} reference item(s). They can be included or excluded in Context Manager.')
        if errors:
            messagebox.showwarning('Some files were skipped', '\n'.join(errors[:8]), parent=self.window)

    def import_history(self):
        path = filedialog.askopenfilename(title='Import saved history or a role-labeled transcript', filetypes=[
            ('History backups and transcripts', '*.zip *.json *.jsonl *.txt *.md *.log'), ('All files', '*.*')], parent=self.window)
        if not path:
            return
        try:
            count = self.store.import_backup(Path(path))
            self.refresh_everything()
            messagebox.showinfo('History imported', f'Imported {count} entries. Choose them in Context Manager.', parent=self.window)
        except Exception as exc:
            messagebox.showerror('History import failed', str(exc), parent=self.window)

    def export_history(self, all_conversations: bool):
        filename = 'local_ai_all_history.zip' if all_conversations else 'local_ai_conversation.zip'
        path = filedialog.asksaveasfilename(title='Save history as a portable ZIP backup', initialfile=filename,
                                            defaultextension='.zip', filetypes=[('ZIP backup', '*.zip')], parent=self.window)
        if not path:
            return
        try:
            self.store.export_zip(Path(path), all_conversations)
            messagebox.showinfo('Saved', f'History saved locally:\n{path}', parent=self.window)
        except Exception as exc:
            messagebox.showerror('Export failed', str(exc), parent=self.window)

    def clear_all_history(self):
        if not messagebox.askyesno('Delete all history?', 'Delete all conversations, selectable context items, and stored image attachments? Export first if you need a backup.', icon='warning', parent=self.window):
            return
        answer = simpledialog.askstring('Final confirmation', 'Type DELETE ALL to permanently erase local history:', parent=self.window)
        if answer != 'DELETE ALL':
            return
        if not messagebox.askyesno('Separate persistent memory',
                'Delete ALL saved conversation history, legacy v0.3 history.json, and local attachments?\n'
                'Persistent memories remain until deleted separately in Memory Manager.', parent=self.window):
            return
        self.store.clear_all()
        self.refresh_everything()
        self.status.set('All saved history cleared. Previously exported backup files are unaffected.')

    def open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(path))
        else:
            messagebox.showinfo('Folder location', str(path), parent=self.window)

    def _validate_mount(self, path: Path):
        resolved = Path(os.path.realpath(path))
        if not resolved.is_dir():
            raise ValueError('Selected folder does not exist.')
        if resolved == Path(resolved.anchor):
            raise ValueError('Mounting a whole drive is not permitted.')
        restricted = [ROOT / 'state', ROOT, Path.home(), Path.home() / '.ssh', Path.home() / '.aws']
        if resolved in ROOT.parents:
            raise ValueError('Cannot authorize a parent folder containing sandbox settings/history.')
        for envname in ('WINDIR', 'ProgramFiles', 'ProgramFiles(x86)', 'APPDATA', 'LOCALAPPDATA'):
            v = os.environ.get(envname)
            if v:
                restricted.append(Path(v))
        for restricted_path in restricted:
            restricted_path = Path(os.path.realpath(restricted_path))
            # Never mount a protected directory itself or something inside a protected system tree.
            if resolved == restricted_path:
                raise ValueError('This broad or protected folder cannot be mounted.')
            if restricted_path in (Path.home() / '.ssh', Path.home() / '.aws', Path.home() / 'AppData'):
                if restricted_path in resolved.parents:
                    raise ValueError('Private user configuration folders cannot be mounted.')
            if restricted_path.name.lower() in ('windows', 'program files', 'program files (x86)'):
                if restricted_path in resolved.parents:
                    raise ValueError('Windows system folders cannot be mounted.')
        if ROOT / 'state' in resolved.parents:
            raise ValueError('Sandbox history / agent settings cannot be mounted.')
        return resolved

    def add_mount(self):
        path = filedialog.askdirectory(title='Authorize one extra Windows folder for Pi', parent=self.window)
        if not path:
            return
        try:
            p = self._validate_mount(Path(path))
            if any(os.path.normcase(m['path']) == os.path.normcase(str(p)) for m in self.mounts):
                raise ValueError('This folder is already authorized.')
            alias = uuid.uuid4().hex[:12]
            self.mounts.append({'id': alias, 'path': str(p), 'mode': 'ro'})
            save_mounts(self.mount_path, self.mounts)
            self.render_mounts()
            self.status.set('Read-only Pi folder access saved. Restart Pi to apply changes.')
        except Exception as exc:
            messagebox.showerror('Folder not added', str(exc), parent=self.window)

    def render_mounts(self):
        self.mount_tree.delete(*self.mount_tree.get_children())
        for m in self.mounts:
            target = '/review/' if m['mode'] == 'ro' else '/projects/'
            self.mount_tree.insert('', tk.END, iid=m['id'], values=(m['path'], 'Read-only' if m['mode'] == 'ro' else 'Read-write', target + m['id']))

    def toggle_mount(self):
        selected = self.mount_tree.selection()
        if not selected:
            return
        mount = next(x for x in self.mounts if x['id'] == selected[0])
        next_mode = 'rw' if mount['mode'] == 'ro' else 'ro'
        if next_mode == 'rw' and not messagebox.askyesno('Allow Pi to modify these files?',
                                                          'Read-write mode allows Pi to overwrite and DELETE files in this Windows folder. Are you sure?',
                                                          icon='warning', parent=self.window):
            return
        mount['mode'] = next_mode
        save_mounts(self.mount_path, self.mounts)
        self.render_mounts()
        self.status.set('Access updated. Restart Pi for the new mount permissions.')

    def remove_mount(self):
        selected = set(self.mount_tree.selection())
        if not selected:
            return
        self.mounts = [m for m in self.mounts if m['id'] not in selected]
        save_mounts(self.mount_path, self.mounts)
        self.render_mounts()
        self.status.set('Pi folder permission removed. Restart Pi to apply.')


    def _build_requests_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.request_tab = tab
        self.tabs.add(tab, text='Requests')
        ttk.Label(tab, text='Offline keyword suggestions. No match is included in the model request until you explicitly approve it.', wraplength=940).pack(anchor=tk.W, pady=(0, 7))
        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        ttk.Checkbutton(top, text='Offer context matches before Send (ask first)', variable=self.suggest_while_sending).pack(side=tk.LEFT)
        ttk.Button(top, text='Search using draft message', command=self.propose_draft_context).pack(side=tk.RIGHT)
        frame = ttk.Frame(tab)
        frame.pack(fill=tk.X, pady=9)
        self.request_tree = ttk.Treeview(frame, columns=('created', 'source', 'query', 'matches'), show='headings', height=7)
        for key, label, width in [('created', 'Date', 145), ('source', 'Request from', 110), ('query', 'Search', 500), ('matches', 'Matches', 70)]:
            self.request_tree.heading(key, text=label)
            self.request_tree.column(key, width=width, stretch=(key=='query'))
        self.request_tree.pack(fill=tk.X)
        self.request_tree.bind('<<TreeviewSelect>>', self.select_request)
        ttk.Label(tab, text='Review individual matches (Ctrl+click for multiple). They remain excluded until approved:').pack(anchor=tk.W)
        candidate = ttk.Frame(tab)
        candidate.pack(fill=tk.BOTH, expand=True, pady=5)
        self.request_hits = ttk.Treeview(candidate, columns=('chat','role','preview'), show='headings', selectmode='extended', height=7)
        for key, label, width in [('chat','Conversation', 180), ('role','Role',90), ('preview','Candidate context',600)]:
            self.request_hits.heading(key, text=label)
            self.request_hits.column(key,width=width, stretch=(key=='preview'))
        self.request_hits.pack(fill=tk.BOTH, expand=True)
        self.request_hits.bind('<<TreeviewSelect>>', self.preview_request_hit)
        self.request_preview = tk.Text(tab, height=5, wrap='word', state=tk.DISABLED)
        self.request_preview.pack(fill=tk.X, pady=4)
        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=(6,0))
        ttk.Button(row, text='Approve highlighted', command=lambda: self.resolve_request('approve', False)).pack(side=tk.LEFT,padx=3)
        ttk.Button(row, text='Approve ALL candidates', command=lambda: self.resolve_request('approve', True)).pack(side=tk.LEFT,padx=3)
        ttk.Button(row, text='Dismiss request', command=lambda: self.resolve_request('dismiss', False)).pack(side=tk.LEFT,padx=3)
        ttk.Button(row, text='Refresh', command=self.render_requests).pack(side=tk.RIGHT,padx=3)

    def render_requests(self):
        if not hasattr(self, 'request_tree'): return
        self.request_tree.delete(*self.request_tree.get_children())
        self.request_hits.delete(*self.request_hits.get_children())
        for item in self.store.context_inbox.pending(self.store.active['id']):
            self.request_tree.insert('', tk.END, iid=item['id'], values=(item['created_at'][:16].replace('T',' '),
                'Model' if item['from_model'] else 'Local search', item['query'],len(item['item_ids'])))

    def selected_request(self):
        ids = self.request_tree.selection()
        return self.store.context_inbox.get(ids[0]) if ids else None

    def select_request(self, _event=None):
        self.request_hits.delete(*self.request_hits.get_children())
        request = self.selected_request()
        if not request: return
        for eid in request['item_ids']:
            item = self.store.get_item(eid)
            cid = self.store.keyword.get_origin(eid)
            if item and cid:
                self.request_hits.insert('', tk.END, iid=eid,
                    values=(self.store._meta(cid)['name'][:45],item['role'],' '.join(item['content'].split())[:180]))

    def preview_request_hit(self, _event=None):
        chosen = self.request_hits.selection()
        item = self.store.get_item(chosen[0]) if chosen else None
        self.request_preview.configure(state=tk.NORMAL)
        self.request_preview.delete('1.0',tk.END)
        if item: self.request_preview.insert(tk.END,item['content'])
        self.request_preview.configure(state=tk.DISABLED)

    def propose_draft_context(self):
        text = self.prompt.get('1.0',tk.END).strip()
        if not text:
            query = simpledialog.askstring('Search archived chats', 'Keyword search request:', parent=self.window)
            if not query: return
            text = query
        try:
            request = self.store.propose_context(text)
            self.render_requests()
            if request:
                self.request_tree.selection_set(request['id'])
                self.select_request()
                self.status.set(f'{len(request["item_ids"])} related archive entries proposed; review then approve.')
            else:
                self.status.set('No unadmitted history matches found for these keywords.')
        except Exception as exc:
            messagebox.showerror('Context search failed', str(exc), parent=self.window)

    def resolve_request(self, action, all_results):
        request = self.selected_request()
        if not request: return
        chosen = list(self.request_hits.get_children()) if all_results else list(self.request_hits.selection())
        if action == 'approve' and not chosen:
            messagebox.showinfo('Highlight matches', 'Highlight one or more candidates, or use Approve ALL.', parent=self.window)
            return
        if action == 'approve' and not messagebox.askyesno('Admit selected history?',
                f'Include {len(chosen)} specific historical item(s) in this conversation\'s next prompts?\n'
                'They will be transmitted to the LOCAL Ollama model when you send a message.', parent=self.window):
            return
        try:
            self.store.context_inbox.resolve(self.store, request['id'], action=action, selected_ids=chosen)
            self.render_requests()
            self.render_context_tree()
            self.status.set('Context admitted for this conversation.' if action=='approve' else 'Suggestion dismissed.')
        except Exception as exc:
            messagebox.showerror('Context review failed', str(exc), parent=self.window)

    def _build_profiles_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Personas')
        ttk.Label(tab, text='Select or clone a profile. Each conversation chooses its own profile; all changes stay local. Personality prompts NEVER authorize tools.', wraplength=950).pack(anchor=tk.W,pady=(0,6))
        top=ttk.Frame(tab)
        top.pack(fill=tk.X)
        self.persona_picker=ttk.Combobox(top, state='readonly', width=30)
        self.persona_picker.pack(side=tk.LEFT,padx=4)
        self.persona_picker.bind('<<ComboboxSelected>>',self.select_persona_editor)
        for label, cb in [('Use in this chat',self.use_persona),('Clone',self.clone_persona),('Save edits',self.save_persona),('Pi identity...',self.apply_identity_pi),('Delete custom',self.delete_persona),('Import...',self.import_persona),('Export...',self.export_persona)]:
            ttk.Button(top,text=label,command=cb).pack(side=tk.LEFT,padx=2)
        main = ttk.Frame(tab)
        main.pack(fill=tk.BOTH,expand=True,pady=(8,0))
        left=ttk.Frame(main)
        left.pack(side=tk.LEFT,fill=tk.BOTH,expand=True,padx=(0,10))
        right=ttk.Frame(main)
        right.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        self.persona_name=tk.StringVar()
        self.persona_agent_name=tk.StringVar()
        self.persona_user_name=tk.StringVar()
        self.persona_greeting=tk.StringVar()
        self.persona_desc=tk.StringVar()
        self.persona_interests=tk.StringVar()
        self.persona_recall=tk.StringVar()
        self.persona_retrieval=tk.StringVar()
        self.persona_roleplay=tk.BooleanVar()
        self.persona_hstyle=tk.StringVar()
        self.persona_hintensity=tk.DoubleVar()
        self.persona_hinitiative=tk.DoubleVar()
        self.persona_seed=tk.IntVar(value=1)
        self.persona_clock=tk.BooleanVar(value=True)
        self.persona_gap=tk.BooleanVar(value=True)
        self.persona_gmode=tk.StringVar(value='natural')
        self.persona_threshold=tk.IntVar(value=30)
        self.persona_schedule=tk.BooleanVar()
        self.persona_timezone=tk.StringVar(value='local')
        for label,var in [('Profile name (library label)',self.persona_name),
                          ('Agent name (how Qwen introduces itself)',self.persona_agent_name),
                          ('What this agent calls you',self.persona_user_name),
                          ('Greeting template (optional: {user_name}, {agent_name})',self.persona_greeting),
                          ('Description',self.persona_desc),
                          ('Interests (comma-separated)',self.persona_interests)]:
            ttk.Label(left,text=label).pack(anchor=tk.W,pady=(4,0))
            ttk.Entry(left,textvariable=var).pack(fill=tk.X,pady=2)
        ttk.Label(left,text='Character instructions / directness / roleplay').pack(anchor=tk.W,pady=(6,1))
        self.persona_instructions=tk.Text(left,height=9,wrap='word')
        self.persona_instructions.pack(fill=tk.BOTH,expand=True)
        ttk.Checkbutton(left,text='Fictional roleplay character',variable=self.persona_roleplay).pack(anchor=tk.W,pady=5)
        ttk.Label(left,text='Recall policy (experimental means reversible simulated forgetfulness)').pack(anchor=tk.W)
        ttk.Combobox(left,textvariable=self.persona_recall,values=['normal','experimental'],state='readonly').pack(fill=tk.X)
        ttk.Label(left,text='Keyword retrieval policy').pack(anchor=tk.W,pady=(5,0))
        ttk.Combobox(left,textvariable=self.persona_retrieval,values=['ask','manual','approved_only'],state='readonly').pack(fill=tk.X)
        ttk.Label(left,text='Recall experiment seed (0–999999)').pack(anchor=tk.W,pady=(5,0))
        ttk.Entry(left,textvariable=self.persona_seed).pack(fill=tk.X)
        ttk.Label(right,text='HUMOR & SOCIAL AWARENESS',font=('Segoe UI',10,'bold')).pack(anchor=tk.W)
        ttk.Label(right,text='Style').pack(anchor=tk.W,pady=(5,0))
        ttk.Combobox(right,textvariable=self.persona_hstyle,values=['none','dry','playful','sarcastic','puns','absurdist','satirical','teasing'],state='readonly').pack(fill=tk.X)
        ttk.Label(right,text='Humor intensity (0 = serious, 5 = high)').pack(anchor=tk.W,pady=(5,0))
        ttk.Scale(right,from_=0,to=5,variable=self.persona_hintensity,orient=tk.HORIZONTAL).pack(fill=tk.X)
        ttk.Label(right,text='Initiative (0 = only if asked, 3 = frequent)').pack(anchor=tk.W,pady=(4,0))
        ttk.Scale(right,from_=0,to=3,variable=self.persona_hinitiative,orient=tk.HORIZONTAL).pack(fill=tk.X)
        ttk.Label(right,text='TIME AWARENESS',font=('Segoe UI',10,'bold')).pack(anchor=tk.W,pady=(14,0))
        ttk.Checkbutton(right,text='Pass computer clock to this personality',variable=self.persona_clock).pack(anchor=tk.W)
        ttk.Checkbutton(right,text='Tell personality elapsed time between user messages',variable=self.persona_gap).pack(anchor=tk.W)
        ttk.Checkbutton(right,text='Enable day/night personality states',variable=self.persona_schedule).pack(anchor=tk.W)
        ttk.Label(right,text='Reaction to conversational gaps').pack(anchor=tk.W,pady=(5,0))
        ttk.Combobox(right,textvariable=self.persona_gmode,values=['silent','natural','expressive'],state='readonly').pack(fill=tk.X)
        ttk.Label(right,text='Minimum minutes before commenting').pack(anchor=tk.W,pady=(5,0))
        ttk.Entry(right,textvariable=self.persona_threshold).pack(fill=tk.X)
        ttk.Label(right,text='System timezone: local or IANA zone (shared setting)').pack(anchor=tk.W,pady=(6,0))
        ttk.Entry(right,textvariable=self.persona_timezone).pack(fill=tk.X)
        ttk.Button(right,text='Save system timezone',command=self.save_persona_timezone).pack(anchor=tk.W,pady=4)
        ttk.Label(right,text='The real archive is never deleted or modified by fictional recall rules.',wraplength=380).pack(anchor=tk.W,pady=(14,0))

    def apply_identity_pi(self):
        from .persona_store import save_pi_identity
        # Saved profile is authoritative; unsaved editor changes do not apply.
        profile=self.store.personas.get(self.persona_edit_id)
        if not messagebox.askyesno('Apply profile identity to Pi?',
              'Write the SAVED profile name/greeting to workspace/AGENTS.md, preserving other sections? '
              'Pi should read it on its NEXT launch in /workspace. This grants no new tool permissions. '
              'Save profile edits first if needed.',parent=self.window):return
        try:
            destination=save_pi_identity(profile,ROOT/'workspace')
            self.audit.record('profile.change')
            self.status.set('Pi identity saved to '+str(destination)+'. Restart Pi to load it.')
        except Exception as exc:
            messagebox.showerror('Pi identity export failed',str(exc),parent=self.window)

    def refresh_profile_choices(self):
        profiles=self.store.personas.all()
        self.profile_combo.configure(values=list(profiles))
        if hasattr(self, 'persona_picker'):
            self._persona_keys=list(profiles)
            self.persona_picker.configure(values=[profiles[k]['name']+'  ['+k+']' for k in self._persona_keys])
            self._persona_keys=list(profiles)
        if hasattr(self, 'compare_a'):
            self.compare_a.configure(values=list(profiles))
            self.compare_b.configure(values=list(profiles))

    def select_persona_editor(self, _event=None):
        index=self.persona_picker.current()
        if index>=0:
            self.persona_edit_id=self._persona_keys[index]
            self.refresh_persona_editor()

    def refresh_persona_editor(self):
        if not hasattr(self, 'persona_picker'): return
        profiles=self.store.personas.all()
        if self.persona_edit_id not in profiles: self.persona_edit_id='balanced'
        self.persona_picker.current(self._persona_keys.index(self.persona_edit_id))
        p=profiles[self.persona_edit_id]
        self.persona_name.set(p['name'])
        self.persona_agent_name.set(p.get('agent_name', 'Qwen'))
        self.persona_user_name.set(p.get('user_name', ''))
        self.persona_greeting.set(p.get('greeting', ''))
        self.persona_desc.set(p['description'])
        self.persona_interests.set(p['interests'])
        self.persona_roleplay.set(p['roleplay'])
        self.persona_recall.set(p['recall'])
        self.persona_retrieval.set(p['retrieval'])
        self.persona_seed.set(p['recall_seed'])
        self.persona_hstyle.set(p['humor']['style'])
        self.persona_hintensity.set(p['humor']['intensity'])
        self.persona_hinitiative.set(p['humor']['initiative'])
        self.persona_clock.set(p['time']['enabled'])
        self.persona_gap.set(p['time']['gap_tracking'])
        self.persona_gmode.set(p['time']['gap_mode'])
        self.persona_threshold.set(p['time']['minimum_gap_minutes'])
        self.persona_schedule.set(p['time']['schedule_personality'])
        self.persona_timezone.set(self.store.personas.timezone)
        self.persona_instructions.delete('1.0',tk.END)
        self.persona_instructions.insert('1.0',p['instructions'])

    def persona_draft(self):
        return {'name':self.persona_name.get(), 'agent_name':self.persona_agent_name.get(),
            'user_name':self.persona_user_name.get(), 'greeting':self.persona_greeting.get(),
            'description':self.persona_desc.get(),
            'instructions':self.persona_instructions.get('1.0',tk.END).strip(),
            'interests':self.persona_interests.get(), 'roleplay':self.persona_roleplay.get(),
            'recall':self.persona_recall.get(),'retrieval':self.persona_retrieval.get(),
            'recall_seed':int(self.persona_seed.get()),
            'humor': {'intensity':round(self.persona_hintensity.get()),
                      'initiative':round(self.persona_hinitiative.get()),'style':self.persona_hstyle.get()},
            'time': {'enabled':self.persona_clock.get(), 'gap_tracking':self.persona_gap.get(),
                     'schedule_personality':self.persona_schedule.get(), 'gap_mode':self.persona_gmode.get(),
                     'minimum_gap_minutes':int(self.persona_threshold.get())}}

    def save_persona_timezone(self):
        try:
            self.store.personas.set_timezone(self.persona_timezone.get().strip())
            self.status.set('Shared local clock timezone saved.')
        except Exception as exc:
            messagebox.showerror('Timezone error',str(exc),parent=self.window)

    def save_persona(self):
        try:
            from .persona_store import PersonaStore
            draft=PersonaStore.validate(self.persona_draft())
            self.store.personas.update(self.persona_edit_id,draft)
            self.store.personas.set_timezone(self.persona_timezone.get().strip())
            self.refresh_profile_choices()
            self.refresh_persona_editor()
            self.status.set('Custom persona saved. Applies to future prompts using this profile.')
        except Exception as exc:
            messagebox.showerror('Cannot save profile',str(exc)+'\nClone a built-in profile before editing it.',parent=self.window)

    def clone_persona(self):
        try:
            key=self.store.personas.clone(self.persona_edit_id)
            self.persona_edit_id=key
            self.refresh_profile_choices()
            self.refresh_persona_editor()
            self.status.set('Custom copy created. Modify its settings, then Save edits.')
        except Exception as exc:
            messagebox.showerror('Clone failed',str(exc),parent=self.window)

    def delete_persona(self):
        key=self.persona_edit_id
        if not key.startswith('user-'): return
        if not messagebox.askyesno('Delete custom profile?',
              'Delete this profile? Conversations using it will fall back to Balanced unless assigned another profile.',parent=self.window): return
        affected=[c['id'] for c in self.store.conversations if c.get('profile')==key]
        for cid in affected:
            self.store.set_profile(cid,'balanced')
        self.store.personas.remove(key)
        self.persona_edit_id='balanced'
        self.refresh_profile_choices()
        self.refresh_persona_editor()

    def use_persona(self):
        self.store.set_profile(self.store.active['id'],self.persona_edit_id)
        self.refresh_everything()
        self.status.set('Personality selected for this conversation.')

    def import_persona(self):
        filename=filedialog.askopenfilename(title='Import JSON personality',filetypes=[('JSON','*.json')],parent=self.window)
        if filename:
            try:
                self.persona_edit_id=self.store.personas.import_profile(filename)
                self.refresh_profile_choices()
                self.refresh_persona_editor()
            except Exception as exc: messagebox.showerror('Import failed',str(exc),parent=self.window)

    def export_persona(self):
        filename=filedialog.asksaveasfilename(title='Export personality only',defaultextension='.json',
            initialfile='persona.json',filetypes=[('JSON','*.json')],parent=self.window)
        if filename:
            try: self.store.personas.export_profile(self.persona_edit_id,filename)
            except Exception as exc: messagebox.showerror('Export failed',str(exc),parent=self.window)

    def _build_compare_tab(self):
        tab=ttk.Frame(self.tabs,padding=10)
        self.tabs.add(tab,text='Compare')
        ttk.Label(tab,text='Compare ONE prompt under two personalities, with the same frozen clock and selection. Does NOT add messages to chat history.',wraplength=940).pack(anchor=tk.W,pady=(0,7))
        opts=ttk.Frame(tab)
        opts.pack(fill=tk.X)
        self.comp_a=tk.StringVar(value='direct')
        self.comp_b=tk.StringVar(value='witty')
        ttk.Label(opts,text='Profile A').pack(side=tk.LEFT)
        self.compare_a=ttk.Combobox(opts,textvariable=self.comp_a,state='readonly',width=24)
        self.compare_a.pack(side=tk.LEFT,padx=8)
        ttk.Label(opts,text='Profile B').pack(side=tk.LEFT)
        self.compare_b=ttk.Combobox(opts,textvariable=self.comp_b,state='readonly',width=24)
        self.compare_b.pack(side=tk.LEFT,padx=8)
        ttk.Button(opts,text='Compare locally',command=self.start_comparison).pack(side=tk.RIGHT)
        ttk.Label(tab,text='Prompt for both profiles:').pack(anchor=tk.W,pady=(10,2))
        self.comp_prompt=tk.Text(tab,height=4,wrap='word')
        self.comp_prompt.pack(fill=tk.X)
        panes=ttk.Panedwindow(tab,orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH,expand=True,pady=(8,0))
        self.compare_texts=[]
        for title in ('Profile A response','Profile B response'):
            pane=ttk.LabelFrame(panes,text=title,padding=7)
            text=tk.Text(pane,wrap='word',state=tk.DISABLED)
            text.pack(fill=tk.BOTH,expand=True)
            panes.add(pane,weight=1)
            self.compare_texts.append(text)
        ttk.Label(tab,text='Generated responses are saved separately under state/comparisons/ as JSON; tool/file permissions are unchanged.').pack(anchor=tk.W,pady=(7,0))

    def start_comparison(self):
        if self.busy: return
        prompt=self.comp_prompt.get('1.0',tk.END).strip()
        if not prompt:
            messagebox.showinfo('Enter a prompt','Type one question to compare.',parent=self.window)
            return
        a,b=self.comp_a.get(),self.comp_b.get()
        if a==b:
            messagebox.showinfo('Select two profiles','Choose two different behavior profiles.',parent=self.window)
            return
        at=utc_now()
        try:
            self.store.memory_options={'global':self.memory_global.get(),'project':self.memory_project.get(),'handoff':self.memory_handoff.get()}
            messages_a=self.store.build_messages(prompt,profile_override=a,at=at)
            messages_b=self.store.build_messages(prompt,profile_override=b,at=at)
        except Exception as exc:
            messagebox.showerror('Comparison preparation failed',str(exc),parent=self.window)
            return
        self.busy=True
        self.send_btn.configure(state=tk.DISABLED)
        self.status.set('Comparing two profiles locally, one after another...')
        cid=self.store.active['id']
        def worker():
            result={'schema':'local-ai-comparison-1','at':iso_utc(at), 'conversation_id':cid,
                    'prompt':prompt,'profiles':[a,b],'responses':[]}
            for msgs in (messages_a,messages_b):
                started=time.monotonic()
                try: result['responses'].append({'text':self.bridge.ask(msgs),'duration_seconds':round(time.monotonic()-started,3)})
                except Exception as exc: result['responses'].append({'error':str(exc)})
            self.notifications.put(('comparison',result))
        threading.Thread(target=worker,daemon=True).start()

    def finish_comparison(self,result):
        target=ROOT/'state'/'comparisons'
        target.mkdir(parents=True,exist_ok=True)
        path=target/(uuid.uuid4().hex+'.json')
        try:
            _atomic_json(path,result)
            for i,widget in enumerate(self.compare_texts):
                widget.configure(state=tk.NORMAL)
                widget.delete('1.0',tk.END)
                response=result['responses'][i]
                widget.insert('1.0',response.get('text','ERROR: '+response.get('error','unknown')))
                widget.configure(state=tk.DISABLED)
            self.status.set('Profile comparison complete; results saved locally as '+path.name)
        except Exception as exc:
            messagebox.showerror('Could not save comparison',str(exc),parent=self.window)

    def _build_sidebar(self, body):
        frame = ttk.Frame(body, width=250, padding=(0, 8, 10, 0))
        frame.pack(side=tk.LEFT, fill=tk.Y)
        frame.pack_propagate(False)
        ttk.Label(frame, text='CONVERSATIONS', style='Small.TLabel').pack(anchor=tk.W, pady=(0, 6))
        self.conversation_tree = ttk.Treeview(frame, show='tree', selectmode='browse', height=22)
        self.conversation_tree.pack(fill=tk.BOTH, expand=True)
        self.conversation_tree.bind('<<TreeviewSelect>>', self.select_sidebar)
        ttk.Button(frame, text='+ New conversation', command=self.new_conversation).pack(fill=tk.X, pady=(8, 3))
        ttk.Button(frame, text='+ New folder', command=self.create_folder).pack(fill=tk.X, pady=3)
        ttk.Button(frame, text='Move chat to folder...', command=self.move_chat).pack(fill=tk.X, pady=3)
        ttk.Button(frame, text='Branch current chat', command=self.branch_chat).pack(fill=tk.X, pady=3)
        ttk.Button(frame, text='Rename / remove folder...', command=self.folder_actions).pack(fill=tk.X, pady=3)

    def render_sidebar(self):
        tree = self.conversation_tree
        current = self.store.active['id']
        tree.delete(*tree.get_children())
        tree.insert('', 'end', iid='folder:unfiled', text='Unfiled', open=True)
        for folder in self.store.data['folders']:
            tree.insert('', 'end', iid='folder:'+folder['id'], text=folder['name'], open=True)
        for c in self.store.conversations:
            parent = 'folder:'+c.get('folder_id', '') if c.get('folder_id') else 'folder:unfiled'
            if not tree.exists(parent):
                parent = 'folder:unfiled'
            tree.insert(parent, 'end', iid='chat:'+c['id'], text=c['name'][:38] + f"  ({c['item_count']})")
        if tree.exists('chat:'+current):
            tree.selection_set('chat:'+current)
            tree.see('chat:'+current)

    def select_sidebar(self, event=None):
        selected = self.conversation_tree.selection()
        if not selected or not selected[0].startswith('chat:'):
            return
        cid = selected[0][5:]
        if cid == self.store.active['id']:
            return
        self.store.set_active(cid)
        self.context_candidates.clear()
        self.refresh_everything()

    def create_folder(self):
        name = simpledialog.askstring('New folder', 'Folder name:', parent=self.window)
        if name is None:
            return
        try:
            self.store.create_folder(name)
            self.render_sidebar()
        except Exception as exc:
            messagebox.showerror('Folder error', str(exc), parent=self.window)

    def move_chat(self):
        folders = self.store.data['folders']
        options = '0 = Unfiled\n' + '\n'.join(f'{i} = {f["name"]}' for i, f in enumerate(folders, 1))
        index = simpledialog.askinteger('Move conversation',
                         'Select destination folder number:\n'+options, minvalue=0, maxvalue=len(folders), parent=self.window)
        if index is None:
            return
        self.store.move_conversation(self.store.active['id'], folders[index-1]['id'] if index else '')
        self.refresh_everything()

    def folder_actions(self):
        chosen = self.conversation_tree.selection()
        if not chosen or not chosen[0].startswith('folder:') or chosen[0] == 'folder:unfiled':
            messagebox.showinfo('Select a folder', 'Select a named folder in the left tree first.', parent=self.window)
            return
        fid = chosen[0][7:]
        name = simpledialog.askstring('Rename or delete folder',
                    'Enter new folder name, or type DELETE to move its chats into Unfiled and delete the folder:',
                    parent=self.window)
        if name is None:
            return
        try:
            if name == 'DELETE':
                if messagebox.askyesno('Remove folder?', 'Chats will move to Unfiled. No conversations will be deleted.', parent=self.window):
                    self.store.delete_folder(fid)
            else:
                self.store.rename_folder(fid, name)
            self.refresh_everything()
        except Exception as exc:
            messagebox.showerror('Folder error', str(exc), parent=self.window)

    def branch_chat(self):
        item_ids = [x['id'] for x in self.store.active['items']]
        count = simpledialog.askinteger('Branch conversation',
                         f'Copy the first how many messages? (1-{len(item_ids)})\nCancel for no action.',
                         minvalue=1, maxvalue=max(1,len(item_ids)), parent=self.window)
        if count is None:
            return
        self.store.branch(self.store.active['id'], until_id=item_ids[count-1] if item_ids else None)
        self.refresh_everything()

    def change_profile(self, _event=None):
        self.store.set_profile(self.store.active['id'], self.profile_var.get())
        self.status.set('Behavior profile saved for this conversation.')

    def save_project(self):
        try:
            self.store.set_project(self.store.active['id'], self.project_var.get())
            self.status.set('Project memory scope saved for this conversation.')
        except Exception as exc:
            messagebox.showerror('Invalid project name', str(exc), parent=self.window)

    def _build_memory_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Memory')
        ttk.Label(tab, text='Approved memories can be retrieved across conversations. Proposed memories are NEVER used until approved. Memory is separate from history.', wraplength=950).pack(anchor=tk.W, pady=(0, 8))
        opt = ttk.Frame(tab)
        opt.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(opt, text='Use approved global', variable=self.memory_global, command=self.save_memory_policy).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(opt, text='Use project', variable=self.memory_project, command=self.save_memory_policy).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(opt, text='Use handoff notes', variable=self.memory_handoff, command=self.save_memory_policy).pack(side=tk.LEFT, padx=4)
        frame = ttk.Frame(tab)
        frame.pack(fill=tk.BOTH, expand=True)
        self.memory_tree = ttk.Treeview(frame, columns=('status','scope','project','summary','sources'), show='headings', height=13)
        for key, title, width in [('status','Status',110),('scope','Scope',80),('project','Project',130),('summary','Memory text',480),('sources','Sources',70)]:
            self.memory_tree.heading(key,text=title)
            self.memory_tree.column(key,width=width,stretch=(key=='summary'))
        self.memory_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.memory_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.memory_tree.configure(yscrollcommand=sb.set)
        self.memory_tree.bind('<<TreeviewSelect>>', self.preview_memory)
        self.memory_preview = tk.Text(tab, height=6, wrap='word', state=tk.DISABLED)
        self.memory_preview.pack(fill=tk.X, pady=(6, 7))
        actions = ttk.Frame(tab)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text='+ Propose new memory', command=self.add_memory).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Propose from context', command=self.propose_from_context).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Approve / revoke', command=self.toggle_memory_approval).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Enable / disable', command=self.toggle_memory_enabled).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Edit', command=self.edit_memory).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Delete', command=self.delete_memory).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text='Import...', command=self.import_memories).pack(side=tk.RIGHT, padx=3)
        ttk.Button(actions, text='Export...', command=self.export_memories).pack(side=tk.RIGHT, padx=3)

    def save_memory_policy(self):
        policy = {'global': self.memory_global.get(), 'project': self.memory_project.get(),
                  'handoff': self.memory_handoff.get()}
        self.store.set_memory_policy(self.store.active['id'], policy)
        self.status.set('Memory retrieval settings saved for this conversation.')

    def render_memories(self):
        if not hasattr(self,'memory_tree'): return
        self.memory_tree.delete(*self.memory_tree.get_children())
        for item in self.store.memory.items:
            status = 'Pending' if not item['approved'] else ('Approved' if item['enabled'] else 'Disabled')
            self.memory_tree.insert('', tk.END, iid=item['id'], values=(status,item['scope'], item.get('persona_id','') or item['project'],
                 ' '.join(item['text'].split())[:150],len(item['source_ids'])))

    def selected_memory(self):
        ids = self.memory_tree.selection()
        return self.store.memory.get(ids[0]) if ids else None

    def preview_memory(self, _event=None):
        item = self.selected_memory()
        self.memory_preview.configure(state=tk.NORMAL)
        self.memory_preview.delete('1.0',tk.END)
        if item:
            self.memory_preview.insert(tk.END, f"{item['scope']} / {item.get('persona_id','') or item['project']}\n{item['text']}\nSources: {', '.join(item['source_ids'])}")
        self.memory_preview.configure(state=tk.DISABLED)

    def _memory_editor(self, title, initial=''):
        dialog = tk.Toplevel(self.window)
        dialog.title(title)
        dialog.geometry('650x340')
        dialog.transient(self.window)
        dialog.grab_set()
        ttk.Label(dialog, text='Enter reusable memory (review before approval):').pack(anchor=tk.W, padx=12, pady=8)
        widget = tk.Text(dialog, height=10, wrap='word')
        widget.pack(fill=tk.BOTH, expand=True, padx=12)
        widget.insert('1.0', initial)
        outcome = []
        def confirm():
            outcome.append(widget.get('1.0',tk.END).strip())
            dialog.destroy()
        ttk.Button(dialog, text='Save', command=confirm).pack(pady=8)
        dialog.wait_window()
        return outcome[0] if outcome else None

    def add_memory(self):
        text = self._memory_editor('Propose persistent memory')
        if not text: return
        scope = simpledialog.askstring('Memory scope','global, project, handoff, or persona:', initialvalue='global', parent=self.window)
        if scope is None: return
        try:
            project = self.store.active.get('project','') if scope == 'project' else ''
            self.store.memory.add(text,scope=scope.strip().lower(),project=project,
                                  persona_id=self.store.active.get('profile','balanced') if scope.strip().lower()=='persona' else '')
            self.render_memories()
            self.status.set('Proposed memory saved but not used until approved.')
        except Exception as exc:
            messagebox.showerror('Memory error',str(exc),parent=self.window)

    def propose_from_context(self):
        ids = list(self.context_tree.selection())
        if not ids:
            messagebox.showinfo('Select history first', 'Select one or more entries in Context Manager.', parent=self.window)
            return
        items = [self.store.get_item(i) for i in ids[:12]]
        text = '\n\n'.join(f"{it['role']}: {it['content'][:800]}" for it in items if it)[:3900]
        revised = self._memory_editor('Review memory proposal', text)
        if not revised: return
        self.store.memory.add(revised,scope='handoff',source_ids=ids[:12],label='Context handoff')
        self.render_memories()
        self.status.set('Context handoff proposed; approval required before reuse.')

    def toggle_memory_approval(self):
        m = self.selected_memory()
        if not m:return
        next_approval = not m['approved']
        self.store.memory.modify(m['id'], approved=next_approval, enabled=True if next_approval else m['enabled'])
        self.render_memories()

    def toggle_memory_enabled(self):
        m = self.selected_memory()
        if not m:return
        self.store.memory.modify(m['id'], enabled=not m['enabled'])
        self.render_memories()

    def edit_memory(self):
        m = self.selected_memory()
        if not m:return
        text=self._memory_editor('Edit memory (requires reapproval)',m['text'])
        if text is not None:
            try:
                self.store.memory.modify(m['id'],text=text)
                self.render_memories()
            except Exception as exc:
                messagebox.showerror('Memory error',str(exc),parent=self.window)

    def delete_memory(self):
        m = self.selected_memory()
        if m and messagebox.askyesno('Delete memory?', 'Delete this saved memory permanently? This will not erase the source conversation.', parent=self.window):
            self.store.memory.remove(m['id'])
            self.render_memories()

    def export_memories(self):
        path=filedialog.asksaveasfilename(title='Export persistent memory JSON', initialfile='sandbox_memories.json',
                                          defaultextension='.json', filetypes=[('JSON', '*.json')], parent=self.window)
        if not path:return
        try:
            n=self.store.memory.export_json(Path(path))
            self.status.set(f'Exported {n} memories. This backup is independent from chat history.')
        except Exception as exc:
            messagebox.showerror('Memory export failed',str(exc),parent=self.window)

    def import_memories(self):
        path=filedialog.askopenfilename(title='Import memory JSON as unapproved candidates',filetypes=[('JSON', '*.json')],parent=self.window)
        if not path:return
        try:
            n=self.store.memory.import_json(Path(path))
            self.render_memories()
            self.status.set(f'Imported {n} memory candidates. Approve individually to use them.')
        except Exception as exc:
            messagebox.showerror('Memory import failed',str(exc),parent=self.window)

    def _build_tools_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text='Tools')
        ttk.Label(tab, text='Managed Ollama Chat has bounded built-in tools. Pi scripts below are a separate agent capability.',wraplength=940).pack(anchor=tk.W,pady=(0,6))
        permissions=ttk.LabelFrame(tab,text='Managed chat permissions (NOT Pi)',padding=7)
        permissions.pack(fill=tk.X,pady=(0,6))
        self.tool_vars={key:tk.BooleanVar(value=value) for key,value in self.tool_permissions.options.items()}
        descriptions=[('calculator','Use safe offline calculator'),('local_clock','Read local system clock'),
                      ('calendar_read','Read saved local calendar events'),
                      ('calendar_propose','Propose events (approval still required)')]
        for key,label in descriptions:
            ttk.Checkbutton(permissions,text=label,variable=self.tool_vars[key]).pack(side=tk.LEFT,padx=(0,12))
        ttk.Button(permissions,text='Save permissions',command=self.save_tool_permissions).pack(side=tk.RIGHT)
        calculator=ttk.LabelFrame(tab,text='Verified offline calculator (manual test)',padding=6)
        calculator.pack(fill=tk.X,pady=(0,8))
        self.math_expression=tk.StringVar(value='2 + 2')
        self.math_result=tk.StringVar(value='')
        ttk.Entry(calculator,textvariable=self.math_expression,width=28).pack(side=tk.LEFT,fill=tk.X,expand=True,padx=4)
        ttk.Button(calculator,text='Compute',command=self.calculate_direct).pack(side=tk.LEFT,padx=5)
        ttk.Label(calculator,textvariable=self.math_result,width=29).pack(side=tk.LEFT,padx=5)
        ttk.Label(tab, text='Pi tools: drop Linux-compatible Python scripts into workspace/tools. Listing a script does not grant Managed Chat permission to execute it.', wraplength=960).pack(anchor=tk.W,pady=(2,6))
        frame = ttk.Frame(tab)
        frame.pack(fill=tk.BOTH, expand=True)
        self.tools_tree = ttk.Treeview(frame,columns=('path','desc'),show='headings')
        for k,name,width in [('path','Python script',370),('desc','Execution',380)]:
            self.tools_tree.heading(k,text=name)
            self.tools_tree.column(k,width=width)
        self.tools_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Label(tab,text='The installed Pi image must include python3-venv and python3-pip. Build Pi v2 once with Allow download; dependencies install from workspace/wheels without internet.').pack(anchor=tk.W,pady=(9,4))
        btn=ttk.Frame(tab)
        btn.pack(fill=tk.X,pady=4)
        ttk.Button(btn,text='Open tools folder',command=lambda:self.open_folder(ROOT/'workspace'/'tools')).pack(side=tk.LEFT,padx=4)
        ttk.Button(btn,text='Refresh tools',command=self.render_tools).pack(side=tk.LEFT,padx=4)
        ttk.Button(btn,text='Copy Pi command',command=self.copy_tool_command).pack(side=tk.LEFT,padx=4)
        ttk.Button(btn,text='Initialize offline Pi .venv',command=self.initialize_pi_venv).pack(side=tk.LEFT,padx=4)

    def render_tools(self):
        if not hasattr(self,'tools_tree'):return
        self.tools_tree.delete(*self.tools_tree.get_children())
        root = ROOT/'workspace'/'tools'
        root.mkdir(parents=True,exist_ok=True)
        for path in sorted(root.rglob('*.py'))[:200]:
            if path.is_symlink() or any(part.startswith('.') for part in path.relative_to(root).parts):
                continue
            rel = path.relative_to(ROOT/'workspace').as_posix()
            self.tools_tree.insert('',tk.END,values=(rel,'Run only when you ask Pi to execute it'))

    def copy_tool_command(self):
        choice=self.tools_tree.selection()
        if not choice:return
        target=self.tools_tree.item(choice[0], 'values')[0]
        command=f'/workspace/.venv/bin/python /workspace/{target}'
        self.window.clipboard_clear()
        self.window.clipboard_append(command)
        self.status.set('Copied Linux sandbox command: '+command)

    def initialize_pi_venv(self):
        import subprocess
        if not messagebox.askyesno('Initialize Pi Python?',
              'Create .venv in the Windows workspace using Linux Python in the Pi container?\n'
              'Only offline cached wheels will be installed. This does not expose the internet.',parent=self.window):
            return
        self.status.set('Creating Linux .venv in background; see result when finished...')
        def worker():
            try:
                proc=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                                    '-File',str(ROOT/'OllamaSandbox.ps1'),'-Action','venv'],
                                    capture_output=True,text=True,timeout=180)
                success=proc.returncode==0
                details=(proc.stdout+'\n'+proc.stderr)
                self.notifications.put(('venv',None,None,None,(success,details)))
            except Exception as exc:
                self.notifications.put(('venv',None,None,None,(False,str(exc))))
        threading.Thread(target=worker,daemon=True).start()


    def calculate_direct(self):
        try:
            self.math_result.set('= '+calculate(self.math_expression.get()))
            self.audit.record('tool.calculate')
        except Exception as exc:
            self.math_result.set('Error: '+str(exc)[:85])
            self.audit.record('tool.calculate',status='error')

    def save_tool_permissions(self):
        try:
            new = {key: value.get() for key, value in self.tool_vars.items()}
            # Per-tool permissions are saved by an explicit user click.
            self.tool_permissions.update(new)
            self.status.set('Managed Ollama Chat tool permissions saved. Pi permissions are separate.')
        except Exception as exc:
            messagebox.showerror('Tool permission error', str(exc), parent=self.window)

    def _build_calendar_tab(self):
        tab = ttk.Frame(self.tabs, padding=9)
        self.tabs.add(tab, text='Calendar')
        ttk.Label(tab, text='Offline calendar. Model tools can READ when permitted or PROPOSE; only you can approve/add/delete real events.',wraplength=960).pack(anchor=tk.W,pady=(0,7))
        body=ttk.Frame(tab)
        body.pack(fill=tk.BOTH,expand=True)
        left=ttk.Frame(body)
        left.pack(side=tk.LEFT,fill=tk.Y,padx=(0,11))
        head=ttk.Frame(left)
        head.pack(fill=tk.X,pady=(0,4))
        ttk.Button(head,text='<',width=3,command=lambda:self.calendar_move_month(-1)).pack(side=tk.LEFT)
        self.calendar_month_text=tk.StringVar()
        ttk.Label(head,textvariable=self.calendar_month_text,font=('Segoe UI',11,'bold')).pack(side=tk.LEFT,expand=True,padx=9)
        ttk.Button(head,text='>',width=3,command=lambda:self.calendar_move_month(1)).pack(side=tk.RIGHT)
        grid=ttk.Frame(left)
        grid.pack(fill=tk.X)
        for col, day_name in enumerate(('Mon','Tue','Wed','Thu','Fri','Sat','Sun')):
            ttk.Label(grid,text=day_name,width=7,anchor=tk.CENTER).grid(row=0,column=col)
        self.calendar_buttons=[]
        for week in range(6):
            row=[]
            for day in range(7):
                button=tk.Button(grid,text='',width=6,height=2,
                         command=lambda r=week,c=day:self.calendar_select_day(r,c))
                button.grid(row=week+1,column=day,padx=1,pady=1)
                row.append(button)
            self.calendar_buttons.append(row)
        self.calendar_dates=[]
        self.calendar_day_text=tk.StringVar()
        ttk.Label(left,textvariable=self.calendar_day_text,wraplength=360).pack(anchor=tk.W,pady=(8,3))
        right=ttk.Frame(body)
        right.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        row=ttk.Frame(right)
        row.pack(fill=tk.X)
        ttk.Label(row,text='Events on selected date',font=('Segoe UI',11,'bold')).pack(side=tk.LEFT)
        ttk.Button(row,text='Refresh',command=self.render_calendar).pack(side=tk.RIGHT)
        self.calendar_events=ttk.Treeview(right,columns=('time','title','notes'),show='headings',height=7)
        for col,name,width in [('time','Start (UTC)',190),('title','Title',190),('notes','Description',350)]:
            self.calendar_events.heading(col,text=name)
            self.calendar_events.column(col,width=width,stretch=(col=='notes'))
        self.calendar_events.pack(fill=tk.BOTH,expand=True,pady=5)
        ttk.Button(right,text='Delete selected event...',command=self.calendar_delete).pack(anchor=tk.E)
        editor=ttk.LabelFrame(right,text='Add calendar event (direct user action)',padding=7)
        editor.pack(fill=tk.X,pady=(6,4))
        self.cal_title=tk.StringVar()
        self.cal_start=tk.StringVar()
        self.cal_end=tk.StringVar()
        self.cal_details=tk.StringVar()
        for label,variable,initial in [('Event title',self.cal_title,''),
           ('Start (YYYY-MM-DD HH:MM)',self.cal_start,dt.datetime.now().strftime('%Y-%m-%d %H:00')),
           ('End (optional; default +1 hour)',self.cal_end,''),('Notes',self.cal_details,'')]:
            row=ttk.Frame(editor)
            row.pack(fill=tk.X,pady=2)
            ttk.Label(row,text=label,width=32).pack(side=tk.LEFT)
            variable.set(initial)
            ttk.Entry(row,textvariable=variable).pack(side=tk.LEFT,fill=tk.X,expand=True)
        actions=ttk.Frame(editor)
        actions.pack(fill=tk.X,pady=(5,0))
        ttk.Button(actions,text='Add event',command=self.calendar_add).pack(side=tk.LEFT,padx=3)
        ttk.Button(actions,text='Export JSON...',command=self.calendar_export).pack(side=tk.LEFT,padx=3)
        ttk.Button(actions,text='Import JSON...',command=self.calendar_import).pack(side=tk.LEFT,padx=3)
        ttk.Button(actions,text='Share next 14 days with Pi...',command=self.calendar_share_pi).pack(side=tk.RIGHT,padx=3)
        pending=ttk.LabelFrame(tab,text='Pending event proposals (never scheduled until approved)',padding=7)
        pending.pack(fill=tk.X,pady=(7,0))
        self.calendar_pending=ttk.Treeview(pending,columns=('title','start','details'),show='headings',height=3)
        for col,name,width in [('title','Proposed event',200),('start','Time (UTC)',200),('details','Notes',450)]:
            self.calendar_pending.heading(col,text=name)
            self.calendar_pending.column(col,width=width,stretch=(col=='details'))
        self.calendar_pending.pack(fill=tk.X)
        row=ttk.Frame(pending)
        row.pack(fill=tk.X,pady=(4,0))
        ttk.Button(row,text='Approve selected...',command=lambda:self.calendar_resolve(True)).pack(side=tk.LEFT,padx=4)
        ttk.Button(row,text='Dismiss selected',command=lambda:self.calendar_resolve(False)).pack(side=tk.LEFT,padx=4)

    def calendar_move_month(self, delta):
        year=self.calendar_month.year
        month=self.calendar_month.month+delta
        if month<1:year,month=year-1,12
        if month>12:year,month=year+1,1
        if 1900<=year<=2100:
            self.calendar_month=dt.date(year,month,1)
            self.render_calendar()

    def calendar_select_day(self, row, col):
        day=self.calendar_dates[row][col]
        if day.month!=self.calendar_month.month:return
        self.calendar_selected=day
        self.cal_start.set(day.strftime('%Y-%m-%d')+' 09:00')
        self.render_calendar()

    def render_calendar(self):
        if not hasattr(self,'calendar_events'):return
        self.calendar_month_text.set(self.calendar_month.strftime('%B %Y'))
        self.calendar_dates=pycalendar.Calendar(firstweekday=0).monthdatescalendar(self.calendar_month.year,self.calendar_month.month)
        while len(self.calendar_dates)<6:
            self.calendar_dates.append([self.calendar_dates[-1][-1]+dt.timedelta(days=k) for k in range(1,8)])
        counts=self.calendar.counts_for_month(self.calendar_month.year,self.calendar_month.month,
                                               timezone=self.store.personas.timezone)
        for row in range(6):
            for col in range(7):
                day=self.calendar_dates[row][col]
                btn=self.calendar_buttons[row][col]
                valid=day.month==self.calendar_month.month
                count=counts.get(day.day,0) if valid else 0
                selected=day==self.calendar_selected
                btn.config(text=(str(day.day)+(f' ({count})' if count else '')) if valid else '',
                           state=tk.NORMAL if valid else tk.DISABLED,
                           relief=tk.SUNKEN if selected else tk.RAISED)
        self.calendar_day_text.set('Selected: '+self.calendar_selected.isoformat()+
          ' | Calendar is local; event descriptions are not in audit logs.')
        self.calendar_events.delete(*self.calendar_events.get_children())
        for event in self.calendar.on_date(self.calendar_selected,self.store.personas.timezone):
            self.calendar_events.insert('',tk.END,iid=event['id'],values=(event['start_utc'],event['title'],event['details'][:220]))
        self.calendar_pending.delete(*self.calendar_pending.get_children())
        for event in self.calendar.pending():
            self.calendar_pending.insert('',tk.END,iid=event['id'],values=(event['title'],event['start_utc'],event['details'][:220]))

    def calendar_add(self):
        try:
            evt=self.calendar.add(self.cal_title.get(),self.cal_start.get(),self.cal_end.get().strip() or None,
                                  self.cal_details.get(),timezone=self.store.personas.timezone)
            self.audit.record('calendar.create')
            from .temporal import local_clock
            self.calendar_selected=local_clock(dt.datetime.fromisoformat(evt['start_utc']),self.store.personas.timezone).date()
            self.calendar_month=self.calendar_selected.replace(day=1)
            self.render_calendar()
            self.status.set('Calendar event saved locally: '+evt['title'])
        except Exception as exc:
            messagebox.showerror('Calendar event error',str(exc),parent=self.window)

    def calendar_delete(self):
        selected=self.calendar_events.selection()
        if not selected:return
        if not messagebox.askyesno('Delete calendar event?', 'Permanently delete the selected event from the local calendar?', parent=self.window):return
        try:
            self.calendar.delete(selected[0])
            self.audit.record('calendar.delete')
            self.render_calendar()
        except Exception as exc:
            messagebox.showerror('Calendar deletion failed',str(exc),parent=self.window)

    def calendar_resolve(self,approve):
        selected=self.calendar_pending.selection()
        if not selected:return
        event=next((x for x in self.calendar.pending() if x['id']==selected[0]),None)
        if event is None:return
        text=(('Approve and schedule' if approve else 'Dismiss')+' this proposed event?\n\n'+
              'Title: '+event['title']+'\nStart (UTC): '+event['start_utc']+
              '\nEnd (UTC): '+event['end_utc']+'\nNotes: '+(event['details'] or '(none)'))
        if not messagebox.askyesno('Review AI calendar proposal',text,parent=self.window):return
        try:
            self.calendar.resolve(selected[0],approve)
            self.audit.record('calendar.approve' if approve else 'calendar.dismiss')
            self.render_calendar()
        except Exception as exc:
            messagebox.showerror('Calendar approval failed',str(exc),parent=self.window)

    def calendar_share_pi(self):
        events=self.calendar.events(days=14,timezone=self.store.personas.timezone)
        prompt=(f'Copy {len(events)} event(s) scheduled for the next 14 days into /workspace/approved_calendar.json?\n\n'
              'Pi can read, change or delete THIS COPY. The original calendar stays outside its workspace. '
              'The shared file includes titles, exact times and any private notes.')
        if not messagebox.askyesno('Calendar sharing permission',prompt,parent=self.window):return
        data={'schema':'local-ai-approved-calendar-1','generated_utc':iso_utc(),
              'coverage_days':14,'events':events}
        target=ROOT/'workspace'/'approved_calendar.json'
        _atomic_json(target,data)
        self.status.set(f'Shared a 14-day calendar snapshot ({len(events)} events) with Pi. Update it manually after changes.')

    def calendar_export(self):
        path=filedialog.asksaveasfilename(title='Export local calendar',defaultextension='.json',
                  filetypes=[('Calendar JSON','*.json')],parent=self.window)
        if not path:return
        self.calendar.export_json(Path(path))
        self.audit.record('calendar.export')
        self.status.set('Calendar exported. Backup may contain private event descriptions.')

    def calendar_import(self):
        path=filedialog.askopenfilename(title='Import local calendar',filetypes=[('Calendar JSON','*.json')],parent=self.window)
        if not path:return
        if not messagebox.askyesno('Import calendar events?', 'Import events from this local JSON file? Imported entries become active calendar events.',parent=self.window):return
        try:
            count=self.calendar.import_json(Path(path))
            self.audit.record('calendar.import')
            self.render_calendar()
            self.status.set(f'Imported {count} calendar events.')
        except Exception as exc:
            messagebox.showerror('Calendar import failed',str(exc),parent=self.window)

    def _build_audit_tab(self):
        tab=ttk.Frame(self.tabs,padding=12)
        self.tabs.add(tab,text='Audit')
        ttk.Label(tab,text='Privacy-first metadata audit: NO chat text, calculator expressions, tool arguments/results, calendar titles or notes are recorded here.',wraplength=970).pack(anchor=tk.W,pady=(0,8))
        settings=ttk.LabelFrame(tab,text='Audit trail preferences',padding=9)
        settings.pack(fill=tk.X)
        self.audit_enabled=tk.BooleanVar(value=self.audit.enabled)
        self.audit_retention=tk.IntVar(value=self.audit.retention_days)
        row=ttk.Frame(settings)
        row.pack(fill=tk.X)
        ttk.Checkbutton(row,text='Save minimal local audit events',variable=self.audit_enabled).pack(side=tk.LEFT,padx=(0,15))
        ttk.Label(row,text='Retention (days, 1-365):').pack(side=tk.LEFT)
        ttk.Entry(row,textvariable=self.audit_retention,width=6).pack(side=tk.LEFT,padx=5)
        ttk.Button(row,text='Save privacy settings',command=self.save_audit_settings).pack(side=tk.LEFT,padx=8)
        ttk.Button(row,text='Delete audit logs...',command=self.clear_audit).pack(side=tk.RIGHT)
        ttk.Label(tab,text='Event and conversation text remains in state/history (and memory in state/memory). These are SEPARATE stores. Exported archives, comparisons, Pi sessions and backup files are not deleted by clearing audit logs.',wraplength=960).pack(anchor=tk.W,pady=(11,6))
        events=ttk.Frame(tab)
        events.pack(fill=tk.BOTH,expand=True)
        self.audit_tree=ttk.Treeview(events,columns=('utc','event','status','session'),show='headings')
        for col,label,width in [('utc','UTC timestamp',205),('event','Operation',210),('status','Outcome',100),('session','Pseudonymous chat',190)]:
            self.audit_tree.heading(col,text=label)
            self.audit_tree.column(col,width=width,stretch=(col=='event'))
        self.audit_tree.pack(fill=tk.BOTH,expand=True)
        actions=ttk.Frame(tab)
        actions.pack(fill=tk.X,pady=(9,0))
        ttk.Button(actions,text='Refresh',command=self.render_audit).pack(side=tk.LEFT,padx=4)
        ttk.Button(actions,text='Export audit metadata...',command=self.export_audit).pack(side=tk.LEFT,padx=4)
        ttk.Button(actions,text='Open audit folder',command=lambda:self.open_folder(ROOT/'state'/'audit')).pack(side=tk.RIGHT,padx=4)

    def save_audit_settings(self):
        try:
            self.audit.configure(enabled=self.audit_enabled.get(),retention_days=self.audit_retention.get())
            self.render_audit()
            self.status.set('Audit preferences saved. History retention is controlled separately.')
        except Exception as exc:
            messagebox.showerror('Audit settings',str(exc),parent=self.window)

    def render_audit(self):
        if not hasattr(self,'audit_tree'):return
        self.audit_tree.delete(*self.audit_tree.get_children())
        for entry in self.audit.recent(250):
            self.audit_tree.insert('',tk.END,values=(entry.get('utc',''),entry.get('event',''),
                      entry.get('status',''),entry.get('conversation_ref','')))

    def clear_audit(self):
        if not messagebox.askyesno('Clear audit events?', 'Delete all saved audit event files? This does NOT delete chat history, local calendar, memories, Pi sessions, comparisons or exported backups.',icon='warning',parent=self.window):return
        self.audit.clear()
        self.render_audit()
        self.status.set('Audit events deleted. Conversation and calendar storage unchanged.')

    def export_audit(self):
        path=filedialog.asksaveasfilename(title='Export redacted audit metadata',defaultextension='.json',
               filetypes=[('JSON','*.json')],parent=self.window)
        if path:
            self.audit.export(Path(path))
            self.render_audit()
            self.status.set('Minimal audit metadata exported. No prompts or calendar details included.')


def main():
    app = tk.Tk()
    LocalChatApp(app)
    app.mainloop()


if __name__ == '__main__':
    main()
