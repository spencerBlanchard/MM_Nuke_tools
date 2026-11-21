"""Run the full file in Nuke 17's Python Script Editor. Render enabled nodes launches a separate Windows console."""
import nuke
import os
import sys
import tempfile
import subprocess
import json
import re
from PySide6 import QtCore, QtGui, QtWidgets, QtNetwork


def discord_settings_path():
    # One user-level settings file shared by every script and Nuke instance.
    return os.path.join(os.path.expanduser("~"), ".nuke", "nuke_render_panel_discord.json")


def validate_webhook_url(value):
    value = value.strip()
    if not re.fullmatch(r"https://(?:discord\.com|discordapp\.com)/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Paste the complete Discord webhook URL copied from your channel.")
    return value


def load_discord_settings():
    try:
        with open(discord_settings_path(), encoding="utf-8") as handle:
            settings = json.load(handle)
        return {"enabled": bool(settings.get("enabled", False)),
                "webhook_url": str(settings.get("webhook_url", ""))}
    except FileNotFoundError:
        return {"enabled": False, "webhook_url": ""}


def save_discord_settings(settings):
    path = discord_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                         dir=os.path.dirname(path)) as handle:
            temporary = handle.name
            json.dump(settings, handle, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.isfile(temporary):
            os.remove(temporary)


class DiscordSettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Discord notifications")
        self.resize(550, 200)
        layout = QtWidgets.QVBoxLayout(self)
        self.enabled = QtWidgets.QCheckBox("Notify when the render queue finishes or fails")
        layout.addWidget(self.enabled)
        layout.addWidget(QtWidgets.QLabel("Webhook URL"))
        self.url = QtWidgets.QLineEdit()
        self.url.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.url.setPlaceholderText("Paste your Discord webhook URL here")
        layout.addWidget(self.url)
        show_url = QtWidgets.QCheckBox("Show URL")
        show_url.toggled.connect(lambda shown: self.url.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if shown else QtWidgets.QLineEdit.EchoMode.Password))
        layout.addWidget(show_url)
        note = QtWidgets.QLabel("Click Save to apply these settings to all projects on this computer.\n"
                                "The URL is stored in a local settings file. No @mentions are sent.\n"
                                "Failure notifications include the last lines of the render log.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.test_status = QtWidgets.QLabel()
        self.test_status.setWordWrap(True)
        layout.addWidget(self.test_status)
        buttons = QtWidgets.QHBoxLayout()
        self.test_button = QtWidgets.QPushButton("Send test")
        self.test_button.setAutoDefault(False)
        self.test_button.clicked.connect(self.send_test)
        buttons.addWidget(self.test_button)
        buttons.addStretch()
        save = QtWidgets.QPushButton("Save")
        save.clicked.connect(self.save_settings)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.network = QtNetwork.QNetworkAccessManager(self)
        try:
            settings = load_discord_settings()
        except (ValueError, OSError, TypeError, AttributeError):
            settings = {"enabled": False, "webhook_url": ""}
            self.test_status.setText("Saved settings could not be read. Enter and save the URL again.")
        self.enabled.setChecked(settings["enabled"])
        self.url.setText(settings["webhook_url"])

    def save_settings(self):
        try:
            url = self.url.text().strip()
            if url or self.enabled.isChecked():
                url = validate_webhook_url(url)
            save_discord_settings({"enabled": self.enabled.isChecked(), "webhook_url": url})
        except (ValueError, OSError) as error:
            QtWidgets.QMessageBox.warning(self, "Could not save Discord settings", str(error))
            return
        self.accept()

    def send_test(self):
        try:
            url = validate_webhook_url(self.url.text())
        except ValueError as error:
            self.test_status.setText(str(error))
            return
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(url + "?wait=true"))
        request.setHeader(QtNetwork.QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setTransferTimeout(15000)
        payload = json.dumps({"content": "Nuke Render Panel: Discord notifications are working.",
                              "allowed_mentions": {"parse": []}}).encode("utf-8")
        self.test_button.setEnabled(False)
        self.test_status.setText("Sending test...")
        reply = self.network.post(request, QtCore.QByteArray(payload))

        def finished():
            status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError and status and 200 <= int(status) < 300:
                self.test_status.setText("Test delivered. Enable notifications and click Save.")
            else:
                self.test_status.setText("Test failed" + (f" (HTTP {status})" if status else "")
                                         + ". Check the URL and internet connection.")
            self.test_button.setEnabled(True)
            reply.deleteLater()

        reply.finished.connect(finished)


# Runs in the independent Windows console, without importing Nuke or Python.
DISCORD_QUEUE_RUNNER = r'''
param([Parameter(Mandatory=$true)][string]$Manifest)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$queue = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$settings = $null
try {
    $settings = Get-Content -LiteralPath $queue.settings_path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
} catch { Write-Host 'Warning: Discord settings could not be read. Rendering will continue.' }
$started = Get-Date
$completed = 0
$failedJob = $null
$errorSummary = ''
$jobLog = ''

function Set-QueueState([string]$phase, [int]$currentIndex) {
    # Replace checkpoints atomically, so the watchdog never reads half a file.
    $state = @{ phase = $phase; completed = $completed; current_index = $currentIndex;
                runner_pid = $PID; started_utc = $started.ToUniversalTime().ToString('o');
                job_log = $jobLog }
    $json = $state | ConvertTo-Json -Compress
    $temporary = $queue.status_path + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    if ([System.IO.File]::Exists($queue.status_path)) {
        # PowerShell coerces bare $null to an empty string for .NET string
        # parameters. File.Replace rejects that as an invalid backup path.
        [System.IO.File]::Replace($temporary, $queue.status_path, [System.Management.Automation.Language.NullString]::Value)
    } else {
        [System.IO.File]::Move($temporary, $queue.status_path)
    }
}

function Send-RenderNotification([string]$message) {
    if (!$settings -or !$settings.enabled -or !$settings.webhook_url) { return }
    $body = @{ content = $message; allowed_mentions = @{ parse = @() } } | ConvertTo-Json -Depth 4 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        try {
            Invoke-RestMethod -Uri ($settings.webhook_url + '?wait=true') -Method Post -ContentType 'application/json' -Body $bytes -TimeoutSec 15 -ErrorAction Stop | Out-Null
            Write-Host 'Discord notification delivered.'
            return
        } catch {
            # Never print the exception: it can contain the private webhook URL.
            if ($attempt -lt 2) { Start-Sleep -Seconds 2 }
        }
    }
    Write-Host 'Warning: Discord notification could not be delivered. Render results are unchanged.'
}

function Get-JobLabel($job) {
    $label = [string]$job.output_name
    if ([string]::IsNullOrWhiteSpace($label)) { $label = [string]$job.name }
    return ('{0} [Write: {1}; frames {2}-{3}]' -f $label, $job.name, $job.first, $job.last)
}

function Send-RenderReport([string]$report) {
    # Keep every shot in the report, even when it needs several Discord messages.
    while ($report.Length -gt 0) {
        $length = [Math]::Min(1800, $report.Length)
        if ($report.Length -gt $length) {
            $newline = $report.LastIndexOf("`n", $length - 1, $length)
            if ($newline -gt 0) { $length = $newline + 1 }
        }
        Send-RenderNotification ($report.Substring(0, $length))
        $report = $report.Substring($length)
    }
}

Set-QueueState 'running' -1
Write-Host 'Rendering at full resolution. You may close Nuke; keep this console open.'
Write-Host ('Logs: ' + $queue.log_directory)
try {
    foreach ($job in $queue.jobs) {
        $failedJob = $job
        $jobLog = Join-Path $queue.log_directory ('job_{0:D3}.log' -f ($completed + 1))
        Set-QueueState 'running' $completed
        Write-Host ('Rendering job {0} of {1}: {2} ({3}-{4})' -f ($completed + 1), @($queue.jobs).Count, $job.name, $job.first, $job.last)
        $renderArgs = @('--indie', '-f', '-X', [string]$job.name, '-F', ('{0}-{1}' -f $job.first, $job.last), [string]$queue.script_path)
        $LASTEXITCODE = 0
        & $queue.executable @renderArgs 2>&1 | Tee-Object -FilePath $jobLog
        $renderExitCode = $LASTEXITCODE
        if ($renderExitCode -ne 0) {
            $errorSummary = 'Nuke exit code: ' + $renderExitCode
            break
        }
        $completed++
        $failedJob = $null
        Set-QueueState 'running' -1
    }
} catch {
    $errorSummary = 'Unable to run render: ' + $_.Exception.Message
}
$elapsed = ((Get-Date) - $started).ToString('hh\:mm\:ss')
$scriptName = [System.IO.Path]::GetFileName($queue.script_path)
$completedLines = @()
$failedLines = @()
$notStartedLines = @()
for ($index = 0; $index -lt @($queue.jobs).Count; $index++) {
    $label = '- ' + (Get-JobLabel $queue.jobs[$index])
    if ($index -lt $completed) {
        $completedLines += $label
    } elseif ($failedJob -and $index -eq $completed) {
        $failedLines += $label
    } else {
        $notStartedLines += $label
    }
}
$shotReport = "`nCOMPLETED ($($completedLines.Count)) - Nuke reported success:`n"
if ($completedLines.Count) { $shotReport += $completedLines -join "`n" } else { $shotReport += '(none)' }
$shotReport += "`n`nFAILED ($($failedLines.Count)) - render again after fixing the error:`n"
if ($failedLines.Count) { $shotReport += $failedLines -join "`n" } else { $shotReport += '(none)' }
$shotReport += "`n`nNOT STARTED ($($notStartedLines.Count)) - still need rendering:`n"
if ($notStartedLines.Count) { $shotReport += $notStartedLines -join "`n" } else { $shotReport += '(none)' }
if ($failedJob -or $errorSummary) {
    $tail = ''
    if ($jobLog -and (Test-Path -LiteralPath $jobLog)) {
        $tail = (Get-Content -LiteralPath $jobLog -Tail 14) -join "`n"
    }
    if ($tail.Length -gt 1100) { $tail = $tail.Substring($tail.Length - 1100) }
    $message = "Render FAILED: $scriptName`nCompleted: $completed / $(@($queue.jobs).Count)`nElapsed: $elapsed`n$errorSummary`n$shotReport`n`nERROR LOG (last lines):`n$tail"
    Write-Host 'Render failed. Remaining jobs were not started.'
    # Only the detached watchdog delivers notifications, preventing duplicates
    # if this console exits while a normal report is being sent.
    [System.IO.File]::WriteAllText($queue.report_path, $message, (New-Object System.Text.UTF8Encoding($false)))
    Set-QueueState 'failed' $(if ($failedJob) { $completed } else { -1 })
    Write-Host 'The background watchdog will send the Discord report.'
    exit 1
}
Write-Host 'All queued renders finished.'
$message = "Render FINISHED: $scriptName`nCompleted: $completed / $(@($queue.jobs).Count)`nElapsed: $elapsed`n$shotReport"
[System.IO.File]::WriteAllText($queue.report_path, $message, (New-Object System.Text.UTF8Encoding($false)))
Set-QueueState 'finished' -1
Write-Host 'The background watchdog will send the Discord report.'
exit 0
'''


# Share the same no-mention delivery, retry, and message-splitting functions.
DISCORD_WATCHDOG = r'''
param([Parameter(Mandatory=$true)][string]$Manifest,
      [Parameter(Mandatory=$true)][int]$ConsolePid)
$ErrorActionPreference = 'Stop'
$queue = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$settings = $null
try {
    $settings = Get-Content -LiteralPath $queue.settings_path -Raw -Encoding UTF8 | ConvertFrom-Json
} catch { }
''' + DISCORD_QUEUE_RUNNER[
    DISCORD_QUEUE_RUNNER.index('function Send-RenderNotification'):
    DISCORD_QUEUE_RUNNER.index("Set-QueueState 'running' -1\nWrite-Host")
] + r'''

function Read-QueueState {
    try { return (Get-Content -LiteralPath $queue.status_path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

function Open-MonitoredProcess([int]$processId) {
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($processId)
        $null = $process.Handle # Pin this process identity against PID reuse.
        return $process
    } catch { return $null }
}

function Has-ProcessExited($process) {
    if ($null -eq $process) { return $true }
    try { $process.Refresh(); return $process.HasExited }
    catch { return $true }
}

$console = Open-MonitoredProcess $ConsolePid
$runner = $null
$state = $null
$watchStarted = Get-Date
$reason = ''
while ($true) {
    $latest = Read-QueueState
    if ($null -ne $latest) { $state = $latest }
    if ($state -and $state.phase -in @('finished', 'failed')) { break }
    if (Has-ProcessExited $console) {
        $reason = 'The render command window exited before recording a normal result.'
        break
    }
    if ($state -and [int]$state.runner_pid -gt 0) {
        if ($null -eq $runner) { $runner = Open-MonitoredProcess ([int]$state.runner_pid) }
        if (Has-ProcessExited $runner) {
            $reason = 'The render queue process exited unexpectedly.'
            break
        }
    } elseif (((Get-Date) - $watchStarted).TotalSeconds -gt 60) {
        $reason = 'The render queue did not start successfully within 60 seconds.'
        break
    }
    Start-Sleep -Seconds 3
}
# Re-read after exit detection: a last checkpoint may have raced with the check.
$latest = Read-QueueState
if ($null -ne $latest) { $state = $latest }
if ($state -and $state.phase -in @('finished', 'failed')) {
    try {
        $report = Get-Content -LiteralPath $queue.report_path -Raw -Encoding UTF8
        Send-RenderReport $report
        exit 0
    } catch { $reason = 'The queue ended, but its saved report could not be read.' }
}
$completed = 0
$currentIndex = -1
if ($state) {
    $completed = [int]$state.completed
    $currentIndex = [int]$state.current_index
}
$scriptName = [System.IO.Path]::GetFileName($queue.script_path)
$message = "Render STOPPED UNEXPECTEDLY: $scriptName`n$reason`n"
$message += 'Completed (last saved checkpoint): ' + $completed + ' / ' + @($queue.jobs).Count + "`n"
$message += "The last active shot is unconfirmed and may have a partial output.`n"
$completedLines = @()
$activeLines = @()
$pendingLines = @()
for ($index = 0; $index -lt @($queue.jobs).Count; $index++) {
    $label = '- ' + (Get-JobLabel $queue.jobs[$index])
    if ($index -lt $completed) { $completedLines += $label }
    elseif ($index -eq $currentIndex) { $activeLines += $label }
    else { $pendingLines += $label }
}
$message += "`nCOMPLETED - Nuke reported success:`n"
if ($completedLines.Count) { $message += $completedLines -join "`n" } else { $message += '(none)' }
$message += "`n`nLAST ACTIVE - completion unconfirmed; check before re-rendering:`n"
if ($activeLines.Count) { $message += $activeLines -join "`n" } else { $message += '(none recorded)' }
$message += "`n`nREMAINING - no saved completion; still need rendering:`n"
if ($pendingLines.Count) { $message += $pendingLines -join "`n" } else { $message += '(none)' }
if ($state -and $state.job_log -and (Test-Path -LiteralPath $state.job_log)) {
    try {
        $tail = (Get-Content -LiteralPath $state.job_log -Tail 14) -join "`n"
        if ($tail.Length -gt 1100) { $tail = $tail.Substring($tail.Length - 1100) }
        $message += "`n`nLAST LOG LINES:`n$tail"
    } catch { }
}
try {
    [System.IO.File]::WriteAllText((Join-Path $queue.log_directory 'interrupted_report.txt'), $message,
                                 (New-Object System.Text.UTF8Encoding($false)))
} catch { }
Send-RenderReport $message
exit 0
'''


def write_discord_render_queue(queue_dir, executable, script_path, jobs, job_labels=None):
    """Keep secrets out of generated queue files; runner reads local settings."""
    runner_path = os.path.join(queue_dir, "render_queue.ps1")
    manifest_path = os.path.join(queue_dir, "render_queue.json")
    status_path = os.path.join(queue_dir, "queue_status.json")
    with open(runner_path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(DISCORD_QUEUE_RUNNER)
    with open(os.path.join(queue_dir, "render_watchdog.ps1"), "w", encoding="utf-8-sig") as handle:
        handle.write(DISCORD_WATCHDOG)
    with open(status_path, "w", encoding="utf-8") as handle:
        json.dump({"phase": "pending", "completed": 0, "current_index": -1,
                   "runner_pid": 0, "job_log": ""}, handle)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"executable": executable, "script_path": script_path,
                   "settings_path": discord_settings_path(), "log_directory": queue_dir,
                   "status_path": status_path, "report_path": os.path.join(queue_dir, "queue_report.txt"),
                   "jobs": [{"name": name, "first": first, "last": last,
                             "output_name": (job_labels or {}).get(name, name)}
                            for name, first, last in jobs]}, handle, ensure_ascii=False)
    powershell = os.path.join(os.environ.get("SystemRoot", "C:/Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.isfile(powershell):
        raise RuntimeError("Windows PowerShell was not found. Turn off Discord notifications to use the standard render queue.")
    return ("@echo off\r\nsetlocal DisableDelayedExpansion\r\nchcp 65001 >nul\r\n"
            "title Nuke Indie Render Queue\r\n" + " ".join([
                batch_arg(powershell), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", batch_arg(runner_path), "-Manifest", batch_arg(manifest_path)])
            + "\r\nendlocal\r\n")


def start_render_watchdog(queue_dir, console_pid):
    powershell = os.path.join(os.environ.get("SystemRoot", "C:/Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    arguments = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", os.path.join(queue_dir, "render_watchdog.ps1"),
                 "-Manifest", os.path.join(queue_dir, "render_queue.json"), "-ConsolePid", str(console_pid)]
    # No shared console: closing the render CMD or Nuke cannot send the watcher
    # their console-close event. Never kill any process from the watchdog.
    with open(os.path.join(queue_dir, "watchdog.log"), "ab") as log:
        return subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                                close_fds=True, cwd=queue_dir)


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
        self.setWindowTitle("Write Node Manager - Tcl v24")
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
        discord_button = QtWidgets.QPushButton("Discord")
        discord_button.setAutoDefault(False)
        discord_button.clicked.connect(self.open_discord_settings)
        execute_row.addWidget(discord_button)
        self.notification_status = QtWidgets.QLabel()
        notification_font = self.notification_status.font()
        notification_font.setPointSize(9)
        self.notification_status.setFont(notification_font)
        execute_row.addWidget(self.notification_status)
        self.update_notification_status()
        # Pick up settings saved from another script or Nuke window, too.
        self.notification_timer = QtCore.QTimer(self)
        self.notification_timer.setInterval(2000)
        self.notification_timer.timeout.connect(self.update_notification_status)
        self.notification_timer.start()
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
        self.watchdog_process = None
        self.table.itemChanged.connect(self.item_changed)
        self.table.cellClicked.connect(self.row_clicked)
        QtCore.QTimer.singleShot(0, self.refresh_nodes)

    def open_discord_settings(self):
        DiscordSettingsDialog(self).exec()
        self.update_notification_status()

    def update_notification_status(self):
        try:
            settings = load_discord_settings()
            enabled = settings["enabled"]
            tooltip = "Shared across all projects on this computer. Change settings with Discord."
        except (ValueError, OSError, TypeError, AttributeError):
            enabled = False
            tooltip = "Discord settings could not be read. Open Discord and save them again."
        self.notification_status.setText("Notifications ON" if enabled else "Notifications OFF")
        self.notification_status.setStyleSheet("color: #74c58a;" if enabled else "color: #e47d7d;")
        self.notification_status.setToolTip(tooltip)

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
            job_labels = {}
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
                job_labels[node.fullName()] = self.table.item(row, 1).text()
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
            try:
                discord = load_discord_settings()
            except (ValueError, OSError, TypeError, AttributeError):
                raise RuntimeError("Discord settings could not be read. Open Discord settings and save them again.")
            if discord["enabled"]:
                validate_webhook_url(discord["webhook_url"])
            # Save current checkbox/range edits before the independent process loads them.
            saved = nuke.scriptSave()
            if saved is False:
                raise RuntimeError("The script was not saved. The render queue was not started.")
            if not os.path.isfile(script_path):
                raise RuntimeError("The saved Nuke script could not be found.")
            queue_dir = tempfile.mkdtemp(prefix="nuke_render_queue_")
            if discord["enabled"]:
                batch = write_discord_render_queue(queue_dir, executable, script_path, jobs, job_labels)
            batch_path = os.path.join(queue_dir, "render_queue.cmd")
            with open(batch_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(batch)
            comspec = os.environ.get("COMSPEC", os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "cmd.exe"))
            command = f'"{comspec}" /d /s /k ""{batch_path}""'
            # No pipes, worker threads or Qt processes tied to the Nuke GUI.
            self.render_process = subprocess.Popen(
                command, creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=os.path.dirname(script_path), close_fds=True)
            if discord["enabled"]:
                try:
                    self.watchdog_process = start_render_watchdog(queue_dir, self.render_process.pid)
                except Exception as error:
                    raise RuntimeError("The render console has started, but its Discord watchdog could not launch. "
                                       "The queue may still be rendering; do not launch it again. "
                                       "Notifications are unavailable for this queue.\n" + str(error)) from error
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
            message.setWindowTitle("Write list failed - Tcl v24")
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
