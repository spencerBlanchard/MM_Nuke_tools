"""Run the full file in Nuke 17's Python Script Editor. Render enabled nodes launches a separate Windows console."""
import nuke
import os
import sys
import tempfile
import subprocess
from PySide6 import QtCore, QtGui, QtWidgets


def tcl_word(value):
    """Quote one Tcl argument without allowing substitutions."""
    value = str(value)
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("$", "\\$"),
                     ("[", "\\["), ("]", "\\]"), ("\n", "\\n"), ("\r", "\\r")):
        value = value.replace(old, new)
    return '"' + value + '"'


def run_tcl_checked(script):
    # Catch inside Tcl: Nuke sometimes drops the error message at the bridge.
    wrapped = (
        "set wm_caught_code [catch {\n" + script +
        "\n} wm_caught_result]\n"
        'if {$wm_caught_code != 0} {\n'
        '    set wm_caught_result "WM_ERROR: $wm_caught_result"\n'
        '    if {[info exists errorInfo]} {append wm_caught_result "\\n$errorInfo"}\n'
        '}\nset wm_caught_result'
    )
    try:
        result = nuke.tcl(wrapped)
    except Exception as error:
        raise RuntimeError(
            "Nuke rejected the Tcl block before returning details. "
            "Command begins: " + script.strip()[:180] + "\n" + repr(error)) from error
    if isinstance(result, str) and result.startswith("WM_ERROR:"):
        raise RuntimeError(result)
    return result


def tcl_call(command, *args):
    return run_tcl_checked(" ".join([command] + [tcl_word(arg) for arg in args]))


class TclKnob:
    """Plain Python data wrapper; never retrieves a Nuke Python Knob."""
    def __init__(self, node, key):
        self.node, self.key = node, key
        self.path = node.ref + "." + key

    def raw(self):
        return str(tcl_call("knob", self.path))

    def value(self):
        return float(tcl_call("numvalue", self.path))

    def evaluate(self):
        # 'in' supplies the Write context for expressions using 'this'.
        return str(tcl_call("in", self.node.ref, "value " + tcl_word(self.path)))

    def setValue(self, value):
        if isinstance(value, bool):
            value = int(value)
        tcl_call("knob", self.path, value)


def is_driven(knob):
    # Only plain scalar states are editable. Curves, expressions, and unknown
    # serializations stay locked rather than being overwritten.
    import re
    return re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false",
                        knob.raw().strip(), re.IGNORECASE) is None


class TclNode:
    """Stores only a Tcl identifier and display name, not a Python Node."""
    def __init__(self, ref, name):
        self.ref, self.name = ref, name

    def fullName(self):
        return self.name

    def __getitem__(self, key):
        return TclKnob(self, key)


def collect_writes():
    # Flat traversal: no namespaces or custom Tcl procedures.
    script = r'''
        set wm_queue [list [list root ""]]
        set wm_result {}
        set wm_seen {}
        set wm_warnings {}
        while {[llength $wm_queue] > 0} {
            set wm_group [lindex [lindex $wm_queue 0] 0]
            set wm_prefix [lindex [lindex $wm_queue 0] 1]
            set wm_queue [lrange $wm_queue 1 end]
            if {[catch {nodes $wm_group} wm_children]} {
                if {[catch {in $wm_group {nodes}} wm_children]} {
                    if {$wm_group eq "root"} {
                        error "Cannot enumerate root nodes: $wm_children"
                    }
                    lappend wm_warnings "Skipped group $wm_group: cannot list its contents. $wm_children"
                    continue
                }
            }
            foreach wm_node $wm_children {
                if {[lsearch -exact $wm_seen $wm_node] >= 0} {continue}
                lappend wm_seen $wm_node
                set wm_name [value $wm_node.name]
                set wm_fullname "${wm_prefix}${wm_name}"
                if {[catch {class $wm_node} wm_type]} {
                    if {[catch {in $wm_node {python {nuke.thisClass()}}} wm_type]} {
                        lappend wm_warnings "Skipped node $wm_fullname: cannot determine class. $wm_type"
                        continue
                    }
                }
                if {$wm_type eq "Write"} {
                    lappend wm_result [list $wm_node $wm_fullname]
                } elseif {$wm_type eq "Group" || $wm_type eq "LiveGroup" ||
                          $wm_type eq "Gizmo" || [exists $wm_node.gizmo_file]} {
                    lappend wm_queue [list "root.${wm_fullname}" "${wm_fullname}."]
                }
            }
        }
        list $wm_result $wm_warnings
    '''
    result = run_tcl_checked(script)
    # Some bridges return native sequences; others return Tcl list strings.
    def unpack(value):
        if isinstance(value, (tuple, list)):
            return list(value)
        if value is None:
            # Nuke returns None for an empty Tcl list/string.
            return []
        return [tcl_call("lindex", value, i)
                for i in range(int(tcl_call("llength", value)))]
    payload = unpack(result)
    if len(payload) != 2:
        raise RuntimeError("Unexpected traversal result: " + repr(result)[:500])
    warnings = [str(item) for item in unpack(payload[1])]
    writes = []
    for entry in unpack(payload[0]):
        fields = unpack(entry)
        if len(fields) != 2:
            raise RuntimeError("Unexpected Tcl node record: " + repr(entry))
        writes.append(TclNode(str(fields[0]), str(fields[1])))
    return writes, warnings


def full_output_path(path):
    """Resolve a relative output against Nuke's project/script directory."""
    path = os.path.expanduser(os.path.expandvars(path))
    if not path:
        return "[No output path]"
    if os.path.isabs(path):
        return os.path.normpath(path)
    script_path = str(tcl_call("value", "root.name") or "")
    base = os.path.dirname(script_path) if os.path.isabs(script_path) else os.getcwd()
    try:
        project_dir = str(tcl_call("value", "root.project_directory") or "")
    except Exception:
        project_dir = ""
    if project_dir:
        project_dir = os.path.expanduser(os.path.expandvars(project_dir))
        base = project_dir if os.path.isabs(project_dir) else os.path.join(base, project_dir)
    return os.path.normpath(os.path.join(base, path))


def batch_arg(value):
    value = str(value)
    if any(char in value for char in ('"', "\r", "\n", "\x00")):
        raise ValueError("A render argument contains an unsupported quote or control character.")
    # Percent expansion occurs even inside quotes in batch scripts.
    return '"' + value.replace("%", "%%") + '"'


def build_render_batch(executable, script_path, jobs):
    lines = ["@echo off", "setlocal DisableDelayedExpansion", "chcp 65001 >nul",
             "title Nuke Indie Render Queue", "echo Rendering enabled Write nodes sequentially.",
             "echo You may close the Nuke GUI. Keep this console open.",
             "echo Do not resave the project while this queue is running.", "echo."]
    for index, (name, first, last) in enumerate(jobs, 1):
        lines.append(f"echo Rendering job {index} of {len(jobs)} - frames {first} to {last}")
        lines.append(" ".join([batch_arg(executable), "--indie", "-f", "-X", batch_arg(name),
                               "-F", batch_arg(f"{first}-{last}"), batch_arg(script_path)]))
        lines.append("if errorlevel 1 goto render_failed")
        lines.append("echo.")
    lines.extend(["echo All queued renders finished.", "goto queue_done", ":render_failed",
                  "echo Render failed. Remaining jobs were not started. See the error above.",
                  ":queue_done", "endlocal"])
    return "\r\n".join(lines) + "\r\n"


def quit_rendering_instance():
    """Quit this GUI process only, after the saved render queue has launched."""
    # Use a Python callback rather than passing Nuke's native callable directly
    # to QTimer. QApplication belongs to this process; other open Nuke
    # instances and the independently launched render console are unaffected.
    app = QtWidgets.QApplication.instance()
    try:
        nuke.scriptExit()
    except Exception as error:
        print("Nuke Render Panel: native exit failed; requesting application quit:", error)
    finally:
        # Some installations return from scriptExit without closing the GUI.
        if app is not None:
            app.quit()


class WriteNodeManager(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Write Node Manager - Tcl v19")
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(850, 440)
        layout = QtWidgets.QVBoxLayout(self)
        heading_row = QtWidgets.QHBoxLayout()
        self.script_name_label = QtWidgets.QLabel("Unsaved script")
        self.script_name_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.script_name_label.setWordWrap(True)
        self.script_name_label.setMinimumWidth(0)
        script_font = self.script_name_label.font()
        script_font.setBold(True)
        self.script_name_label.setFont(script_font)
        heading_row.addWidget(self.script_name_label, 1, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        panel_title = QtWidgets.QLabel("Nuke Render Panel")
        title_font = panel_title.font()
        title_font.setPointSize(18)
        title_font.setBold(True)
        panel_title.setFont(title_font)
        heading_row.addWidget(panel_title, 0, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        heading_row.setContentsMargins(0, 2, 0, 8)
        layout.addLayout(heading_row)
        top_row = QtWidgets.QHBoxLayout()
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.setFixedWidth(85)
        refresh.setAutoDefault(False)
        refresh.clicked.connect(self.refresh_nodes)
        enable_all = self.enable_all_button = QtWidgets.QPushButton("Check All")
        disable_all = self.disable_all_button = QtWidgets.QPushButton("Check None")
        for button in (enable_all, disable_all):
            button.setAutoDefault(False)
            top_row.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        enable_all.clicked.connect(lambda: self.set_all_enabled(True))
        disable_all.clicked.connect(lambda: self.set_all_enabled(False))
        top_row.addStretch()
        top_row.addWidget(refresh, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top_row)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Enabled", "Output filename", "Frame range", "Range source"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setStyleSheet(
            "QHeaderView::section {"
            "background-color: #242424; color: #d0d0d0;"
            "border: none; border-right: 1px solid #383838;"
            "border-bottom: 1px solid #181818; padding: 5px 7px;"
            "}")
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        self.empty_label = QtWidgets.QLabel("No Write nodes in this script.")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()  # Keep internal status bookkeeping out of the interface.
        self.scan_warnings = QtWidgets.QPlainTextEdit()
        self.scan_warnings.setReadOnly(True)
        self.scan_warnings.setMaximumHeight(110)
        self.scan_warnings.hide()
        layout.addWidget(self.scan_warnings)
        execute_row = QtWidgets.QHBoxLayout()
        execute_row.addStretch()
        self.render_button = QtWidgets.QPushButton("Render")
        self.execute_close_button = QtWidgets.QPushButton("Render && close Nuke")
        for button in (self.render_button, self.execute_close_button):
            button.setAutoDefault(False)
            button.setMinimumHeight(30)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            execute_row.addWidget(button)
        self.render_button.clicked.connect(lambda: self.render_enabled(False))
        self.execute_close_button.clicked.connect(lambda: self.render_enabled(True))
        layout.addLayout(execute_row)
        self.render_button.setMinimumWidth(90)
        self.execute_close_button.setMinimumWidth(175)
        self.render_process = None
        self.table.itemChanged.connect(self.item_changed)
        self.table.cellClicked.connect(self.row_clicked)
        QtCore.QTimer.singleShot(0, self.refresh_nodes)

    def render_enabled(self, close_nuke=False):
        try:
            if os.name != "nt":
                raise RuntimeError("Command Prompt rendering is currently Windows-only.")
            if self.render_process is not None and self.render_process.poll() is None:
                raise RuntimeError("The previous render console is still open. Close it before starting another queue.")
            # Read the frame ranges retained with each displayed row.
            self.render_button.setFocus()
            QtWidgets.QApplication.processEvents()
            jobs = []
            for row in range(self.table.rowCount()):
                node = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                if bool(node["disable"].value()):
                    continue
                if is_driven(node["disable"]):
                    raise RuntimeError(f"{node.fullName()}: disable is animated/expression-driven. Set a fixed enable state before queueing.")
                first, last = self.table.item(row, 2).data(QtCore.Qt.ItemDataRole.UserRole)
                if first > last:
                    raise ValueError(f"{node.fullName()}: In must not exceed Out.")
                jobs.append((node.fullName(), first, last))
            if not jobs:
                raise RuntimeError("No enabled Write nodes are listed.")
            if getattr(self, "scan_incomplete", True):
                raise RuntimeError("Refresh must complete without skipped nodes before rendering. See the scan warnings.")
            script_path = str(tcl_call("value", "root.name") or "")
            if not script_path.lower().endswith(".nkind") or not os.path.isabs(script_path):
                raise RuntimeError("Save this project as a .nkind file first, then click Render again.")
            executable = str(getattr(nuke, "EXE_PATH", "") or sys.executable)
            if not os.path.isfile(executable) or not os.path.basename(executable).lower().startswith("nuke"):
                executable, _ = QtWidgets.QFileDialog.getOpenFileName(
                    self, "Choose the Nuke executable", "C:/Program Files", "Nuke executable (*.exe)")
                if not executable:
                    return
            batch = build_render_batch(executable, script_path, jobs)
            # Save current checkbox/range edits before the independent process loads them.
            saved = nuke.scriptSave()
            if saved is False:
                raise RuntimeError("The script was not saved. The render queue was not started.")
            if not os.path.isfile(script_path):
                raise RuntimeError("The saved Nuke script could not be found.")
            queue_dir = tempfile.mkdtemp(prefix="nuke_render_queue_")
            batch_path = os.path.join(queue_dir, "render_queue.cmd")
            with open(batch_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(batch)
            comspec = os.environ.get("COMSPEC", os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "cmd.exe"))
            command = f'"{comspec}" /d /s /k ""{batch_path}""'
            # No pipes, worker threads or Qt processes tied to the Nuke GUI.
            self.render_process = subprocess.Popen(
                command, creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=os.path.dirname(script_path), close_fds=True)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "Could not start render queue", str(error))
            return
        # Close only after the saved script has been handed to a separate console.
        self.close()
        if close_nuke:
            # Quit only the GUI application hosting this panel, not any other
            # Nuke process or the detached render console.
            QtCore.QTimer.singleShot(0, quit_rendering_instance)

    def set_all_enabled(self, enabled):
        # Operate on the displayed Writes; don't overwrite expressions/curves.
        nodes = [self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                 for row in range(self.table.rowCount())]
        if not nodes:
            self.status.setText("No Write nodes are listed.")
            return
        changed = 0
        skipped = []
        failures = []
        undo = nuke.Undo()
        undo.begin("Enable all Writes" if enabled else "Disable all Writes")
        try:
            for node in nodes:
                try:
                    knob = node["disable"]
                    if is_driven(knob):
                        skipped.append(node.fullName())
                        continue
                    if bool(knob.value()) != (not enabled):
                        knob.setValue(not enabled)
                        changed += 1
                except Exception as error:
                    failures.append(f"{node.fullName()}: {error}")
        finally:
            undo.end()
        self.refresh_nodes()
        summary = (f"{'Enabled' if enabled else 'Disabled'} {changed} Write(s). "
                   f"Skipped {len(skipped)} driven control(s); {len(failures)} error(s).")
        self.status.setText(self.status.text() + "\n" + summary)
        details = (["Protected disable controls: " + ", ".join(skipped)] if skipped else []) + failures
        if details:
            existing = self.scan_warnings.toPlainText() if self.scan_warnings.isVisible() else ""
            self.scan_warnings.setPlainText("\n".join(([existing] if existing else []) + details))
            self.scan_warnings.show()

    def row_clicked(self, row, column):
        self.focus_write(row, column)

    def focus_write(self, row, column):
        item = self.table.item(row, 0)
        if item is None:
            return
        node = item.data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            # Read coordinates through Tcl, avoiding Python Node retrievals.
            x = node["xpos"].value()
            y = node["ypos"].value()
            parent = node.fullName().rsplit(".", 1)[0] if "." in node.fullName() else ""
            group = "root." + parent if parent else "root"
            tcl_call("show_dag", group)
            # Match a normal single selection in this graph, using Tcl only.
            selection_script = (
                "foreach wm_selection_node [nodes " + tcl_word(group) + "] {\n"
                "    knob $wm_selection_node.selected 0\n"
                "}\nknob " + tcl_word(node.ref + ".selected") + " 1"
            )
            run_tcl_checked(selection_script)
            # Center near the middle of a standard Write tile at a readable zoom.
            tcl_call("zoom", group, 1.0, x + 40, y + 10)
        except Exception as error:
            self.scan_warnings.setPlainText(f"Could not focus {node.fullName()}: {error}")
            self.scan_warnings.show()

    def refresh_nodes(self):
        try:
            script_path = str(tcl_call("value", "root.name") or "")
            script_name = script_path.replace("\\", "/").rsplit("/", 1)[-1]
            self.script_name_label.setText(script_name if script_name and script_name != "Root" else "Unsaved script")
            self.script_name_label.setToolTip(script_path if script_name and script_name != "Root" else "Save the script to give it a name.")
        except Exception:
            self.script_name_label.setText("Unsaved script")
            self.script_name_label.setToolTip("")
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.scan_incomplete = True
        self.scan_warnings.hide()
        self.empty_label.hide()
        try:
            writes, warnings = collect_writes()
            entries = []
            for node in writes:
                name = node.fullName()
                try:
                    path = node["file"].evaluate()
                    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
                    filename = filename or f"[No output filename] {name}"
                    tooltip = f"Write node: {name}\nOutput: {full_output_path(path)}"
                except Exception as error:
                    filename = f"[Filename error] {name}"
                    tooltip = f"Write node: {name}\nCould not evaluate output:\n{error}"
                entries.append((filename, name, node, tooltip))
            entries.sort(key=lambda entry: (entry[0].lower(), entry[1].lower()))
            for filename, name, node, tooltip in entries:
                row = self.table.rowCount()
                self.table.insertRow(row)
                item = QtWidgets.QTableWidgetItem("")
                item.setFlags((item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                              | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, node)
                item.setCheckState(QtCore.Qt.CheckState.Unchecked if node["disable"].value()
                                   else QtCore.Qt.CheckState.Checked)
                if is_driven(node["disable"]):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    tooltip += "\nDisable is animated or expression-driven; edit it on the node."
                item.setToolTip(tooltip)
                self.table.setItem(row, 0, item)
                name_item = QtWidgets.QTableWidgetItem(filename)
                name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                name_item.setToolTip(tooltip)
                self.table.setItem(row, 1, name_item)
                limited = bool(node["use_limit"].value())
                first = int(node["first"].value() if limited else TclNode("root", "root")["first_frame"].value())
                last = int(node["last"].value() if limited else TclNode("root", "root")["last_frame"].value())
                cell = QtWidgets.QTableWidgetItem(f"{first}-{last}")
                cell.setFlags(cell.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setData(QtCore.Qt.ItemDataRole.UserRole, (first, last))
                cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 2, cell)
                source = QtWidgets.QTableWidgetItem("Write limit" if limited else "Project default")
                source.setFlags(source.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, 3, source)
                for col in range(self.table.columnCount()):
                    self.table.item(row, col).setToolTip(tooltip)
                self.style_row(row, not bool(node["disable"].value()))
            self.sort_rows()
            self.status.setText(
                f"Found {len(writes)} Write node(s) via Tcl. "
                + (f"INCOMPLETE SCAN: {len(warnings)} item(s) skipped. See warnings below."
                   if warnings else "Changes apply immediately. Save to keep them."))
            self.empty_label.setVisible(not writes and not warnings)
            self.scan_incomplete = bool(warnings)
            self.scan_warnings.setPlainText("\n".join(warnings))
            self.scan_warnings.setVisible(bool(warnings))
            if warnings:
                print("Write Manager scan warnings:\n" + "\n".join(warnings))
        except Exception as error:
            self.table.setRowCount(0)
            self.status.setText(f"Could not list Write nodes:\n{error}")
            import traceback
            details = traceback.format_exc()
            print(details)
            message = QtWidgets.QMessageBox(self)
            message.setWindowTitle("Write list failed - Tcl v19")
            message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            message.setText("The Write list could not load.")
            message.setInformativeText(str(error))
            message.setDetailedText(details)
            message.exec()
        finally:
            self.table.blockSignals(False)
            self.update_action_buttons()

    def update_action_buttons(self):
        has_rows = self.table.rowCount() > 0
        has_enabled = any(
            self.table.item(row, 0) is not None
            and self.table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked
            for row in range(self.table.rowCount()))
        self.enable_all_button.setEnabled(has_rows)
        self.disable_all_button.setEnabled(has_rows)
        self.render_button.setEnabled(has_enabled and not getattr(self, "scan_incomplete", True))
        self.execute_close_button.setEnabled(self.render_button.isEnabled())

    def sort_rows(self):
        """Enabled first, then filename; move whole rows with their node data."""
        current = self.table.currentItem()
        scroll = self.table.verticalScrollBar().value()
        rows = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.item(row, 0)
            filename = self.table.item(row, 1)
            if checkbox is None or filename is None:
                return
            node = checkbox.data(QtCore.Qt.ItemDataRole.UserRole)
            key = (checkbox.checkState() != QtCore.Qt.CheckState.Checked,
                   filename.text().casefold(), node.fullName().casefold())
            rows.append((key, row))
        order = [row for key, row in sorted(rows)]
        if order == list(range(self.table.rowCount())):
            return
        previous = self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            cells = [[self.table.takeItem(row, col)
                      for col in range(self.table.columnCount())]
                     for row in range(self.table.rowCount())]
            for new_row, old_row in enumerate(order):
                for col, cell in enumerate(cells[old_row]):
                    self.table.setItem(new_row, col, cell)
            if current is not None:
                self.table.setCurrentItem(current)
            self.table.verticalScrollBar().setValue(scroll)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(previous)

    def style_row(self, row, enabled):
        # Styling emits itemChanged too, so preserve and block signals here.
        previous = self.table.blockSignals(True)
        try:
            background = QtGui.QColor("#383838" if enabled else "#303030")
            foreground = QtGui.QColor("#eeeeee" if enabled else "#929292")
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setBackground(QtGui.QBrush(background))
                    cell.setForeground(QtGui.QBrush(foreground))
                    font = cell.font()
                    font.setBold(enabled and col == 1)
                    cell.setFont(font)
        finally:
            self.table.blockSignals(previous)

    def item_changed(self, item):
        row, col = item.row(), item.column()
        if col != 0:
            return
        node = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            if is_driven(node["disable"]):
                raise ValueError("Disable is animated or expression-driven; edit it on the node.")
            disabled = item.checkState() == QtCore.Qt.CheckState.Unchecked
            node["disable"].setValue(disabled)
            self.style_row(row, not disabled)
            # Preserve row order until Refresh rebuilds and sorts the list.
            self.update_action_buttons()
            self.status.setText(f"{self.table.item(row, 1).text()}: {'disabled' if disabled else 'enabled'}")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "Could not update Write node", str(error))
            self.refresh_nodes()


def show_write_node_manager():
    """Menu entry point; keep one live manager window."""
    global write_node_manager
    try:
        if write_node_manager.isVisible():
            write_node_manager.raise_()
            write_node_manager.activateWindow()
            return write_node_manager
        write_node_manager.close()
    except (NameError, RuntimeError):
        pass
    write_node_manager = WriteNodeManager()
    write_node_manager.show()
    write_node_manager.raise_()
    write_node_manager.activateWindow()
    return write_node_manager


# Still supports executing this entire file in Nuke's Script Editor.
if __name__ == "__main__":
    show_write_node_manager()
