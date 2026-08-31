# ASTAR SECS/GEM - graphical setup.
#
# Started by SETUP.bat, which is the only thing in this package anyone needs
# to double-click. This window asks the one question that actually differs
# between the two machines of an installation - which side is this? - and runs
# install.ps1 with the answer. Nothing here asks anyone to type a path, an IP,
# a port, or a PowerShell command.
#
# WinForms rather than the Python/Tk panels on purpose: on a fresh Windows 11
# machine Python is not installed yet, and this is the thing that installs it.
# .NET and WinForms are always present.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installScript = Join-Path $scriptDir "upgrade.ps1"
if (-not (Test-Path $installScript -PathType Leaf)) {
    [System.Windows.Forms.MessageBox]::Show(
        "upgrade.ps1 is not next to Setup.ps1.`n`nExtract the whole ZIP and run SETUP.bat from the extracted folder, not from inside the ZIP viewer.",
        "Incomplete package", "OK", "Error") | Out-Null
    exit 1
}

$logPath = Join-Path $env:TEMP ("astar-setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

# ---------------------------------------------------------------- layout ---

$form = New-Object System.Windows.Forms.Form
$form.Text = "ASTAR SECS/GEM - Setup"
$form.Size = New-Object System.Drawing.Size(720, 640)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$heading = New-Object System.Windows.Forms.Label
$heading.Text = "What is this computer for?"
$heading.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$heading.Location = New-Object System.Drawing.Point(20, 18)
$heading.Size = New-Object System.Drawing.Size(660, 26)
$form.Controls.Add($heading)

$roleBox = New-Object System.Windows.Forms.GroupBox
$roleBox.Location = New-Object System.Drawing.Point(20, 48)
# 168 clipped the third option's explanation: its label sits at y=150
# and is 30 tall, so the group needs at least 186.
$roleBox.Size = New-Object System.Drawing.Size(660, 190)
$roleBox.Text = "Pick one"
$form.Controls.Add($roleBox)

function New-RoleOption($text, $detail, $top, $checked) {
    $radio = New-Object System.Windows.Forms.RadioButton
    $radio.Text = $text
    $radio.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
    $radio.Location = New-Object System.Drawing.Point(16, $top)
    $radio.Size = New-Object System.Drawing.Size(620, 22)
    $radio.Checked = $checked
    $roleBox.Controls.Add($radio)

    $label = New-Object System.Windows.Forms.Label
    $label.Text = $detail
    $label.ForeColor = [System.Drawing.Color]::DimGray
    $label.Location = New-Object System.Drawing.Point(38, ($top + 22))
    $label.Size = New-Object System.Drawing.Size(600, 30)
    $roleBox.Controls.Add($label)
    return $radio
}

$radioMiddleware = New-RoleOption "The EAP - it collects from machines" `
    "Installs the middleware and its control panel. This is the production choice." 24 $true
$radioSimulator = New-RoleOption "A test machine - it pretends to be a tool" `
    "Installs the simulator and its control panel. Use this on the second computer." 76 $false
$radioBoth = New-RoleOption "Both, on this one computer" `
    "For running both sides on a single computer." 128 $false

# Advanced settings are collapsed into defaults that suit a normal install.
# They are shown, not hidden, so nothing is a surprise - but nobody has to
# touch them.
$optionBox = New-Object System.Windows.Forms.GroupBox
$optionBox.Location = New-Object System.Drawing.Point(20, 248)
$optionBox.Size = New-Object System.Drawing.Size(660, 92)
$optionBox.Text = "Settings (defaults are fine)"
$form.Controls.Add($optionBox)

$dirLabel = New-Object System.Windows.Forms.Label
$dirLabel.Text = "Install to"
$dirLabel.Location = New-Object System.Drawing.Point(16, 28)
$dirLabel.Size = New-Object System.Drawing.Size(70, 22)
$optionBox.Controls.Add($dirLabel)

$dirText = New-Object System.Windows.Forms.TextBox
$dirText.Text = "C:\SECSGEM_EAP"
$dirText.Location = New-Object System.Drawing.Point(90, 25)
$dirText.Size = New-Object System.Drawing.Size(260, 22)
$optionBox.Controls.Add($dirText)

$portLabel = New-Object System.Windows.Forms.Label
$portLabel.Text = "Simulator port"
$portLabel.Location = New-Object System.Drawing.Point(374, 28)
$portLabel.Size = New-Object System.Drawing.Size(90, 22)
$optionBox.Controls.Add($portLabel)

$portText = New-Object System.Windows.Forms.TextBox
$portText.Text = "5051"
$portText.Location = New-Object System.Drawing.Point(470, 25)
$portText.Size = New-Object System.Drawing.Size(70, 22)
$optionBox.Controls.Add($portText)

$portNote = New-Object System.Windows.Forms.Label
$portNote.Text = "The port the simulator listens on. Opened in Windows Firewall automatically."
$portNote.ForeColor = [System.Drawing.Color]::DimGray
$portNote.Location = New-Object System.Drawing.Point(16, 56)
$portNote.Size = New-Object System.Drawing.Size(620, 22)
$optionBox.Controls.Add($portNote)

$installButton = New-Object System.Windows.Forms.Button
$installButton.Text = "Install"
$installButton.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$installButton.Location = New-Object System.Drawing.Point(20, 352)
$installButton.Size = New-Object System.Drawing.Size(140, 34)
$form.Controls.Add($installButton)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Windows will ask for administrator rights once you press Install."
$statusLabel.Location = New-Object System.Drawing.Point(174, 362)
$statusLabel.Size = New-Object System.Drawing.Size(506, 22)
$form.Controls.Add($statusLabel)

$logText = New-Object System.Windows.Forms.TextBox
$logText.Multiline = $true
$logText.ReadOnly = $true
$logText.ScrollBars = "Vertical"
$logText.Font = New-Object System.Drawing.Font("Consolas", 9)
$logText.Location = New-Object System.Drawing.Point(20, 398)
$logText.Size = New-Object System.Drawing.Size(660, 138)
$form.Controls.Add($logText)

$openButton = New-Object System.Windows.Forms.Button
$openButton.Text = "Open the control panel"
$openButton.Location = New-Object System.Drawing.Point(20, 536)
$openButton.Size = New-Object System.Drawing.Size(180, 30)
$openButton.Enabled = $false
$form.Controls.Add($openButton)

$closeButton = New-Object System.Windows.Forms.Button
$closeButton.Text = "Close"
$closeButton.Location = New-Object System.Drawing.Point(600, 536)
$closeButton.Size = New-Object System.Drawing.Size(80, 30)
$form.Controls.Add($closeButton)

# ---------------------------------------------------------------- actions ---

function Get-SelectedRole {
    if ($radioSimulator.Checked) { return "Simulator" }
    if ($radioBoth.Checked) { return "Both" }
    return "Middleware"
}

# Single quotes wrap every value passed into the elevated -Command string, so
# a quote inside a path would end the argument early. Doubling it is the
# PowerShell escape.
function ConvertTo-PsLiteral($value) {
    return "'" + ($value -replace "'", "''") + "'"
}

$script:process = $null
$script:logOffset = 0

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500

function Read-NewLogLines {
    if (-not (Test-Path $logPath)) { return }
    try {
        # @() matters: Get-Content returns a bare string for a one-line file,
        # and range-indexing a string yields characters.
        # -Encoding UTF8 explicitly: auto-detection is what let a
        # mixed-encoding file render as CJK instead of an error message.
        $all = @(Get-Content $logPath -Encoding UTF8 -ErrorAction Stop)
        if ($all.Count -gt $script:logOffset) {
            $fresh = $all[$script:logOffset..($all.Count - 1)]
            $script:logOffset = $all.Count
            $logText.AppendText(($fresh -join "`r`n") + "`r`n")
        }
    } catch {
        # The elevated process holds the file while writing; skip this tick
        # rather than showing the operator a sharing violation.
    }
}

$timer.Add_Tick({
    Read-NewLogLines
    if ($script:process -ne $null -and $script:process.HasExited) {
        $timer.Stop()
        # Once more after exit: the install writes its final lines - the ones
        # saying why it failed - between the last tick and the process ending,
        # and stopping the timer here would drop exactly those.
        Read-NewLogLines
        $code = $script:process.ExitCode
        $script:process = $null
        $installButton.Enabled = $true
        if ($code -eq 0) {
            $statusLabel.Text = "Done. Shortcuts are on the desktop."
            $statusLabel.ForeColor = [System.Drawing.Color]::ForestGreen
            $openButton.Enabled = $true
        } else {
            $statusLabel.Text = "Install failed (exit $code). The log above says why."
            $statusLabel.ForeColor = [System.Drawing.Color]::Firebrick
        }
    }
})

$installButton.Add_Click({
    $port = 0
    if (-not [int]::TryParse($portText.Text.Trim(), [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        [System.Windows.Forms.MessageBox]::Show(
            "The simulator port must be a number between 1 and 65535.",
            "Check the port", "OK", "Warning") | Out-Null
        return
    }
    $dir = $dirText.Text.Trim()
    if ([string]::IsNullOrWhiteSpace($dir)) {
        [System.Windows.Forms.MessageBox]::Show(
            "Enter a folder to install into.", "Check the folder", "OK", "Warning") | Out-Null
        return
    }

    $installButton.Enabled = $false
    $openButton.Enabled = $false
    $logText.Clear()
    $script:logOffset = 0
    $statusLabel.ForeColor = [System.Drawing.Color]::Black
    $statusLabel.Text = "Installing - approve the administrator prompt..."

    # -Verb RunAs cannot be combined with -RedirectStandardOutput, so the
    # elevated process tees its own output to a file that this window tails.
    #
    # The exit code comes from try/catch, never from $LASTEXITCODE: the last
    # native command install.ps1 runs is validate-config, whose non-zero exit
    # is expected and explicitly tolerated ("normal until you edit
    # production.yaml"). Trusting it would report a failed install after a
    # perfectly good one. install.ps1 sets $ErrorActionPreference = "Stop"
    # and throws on every real failure, so a clean return means success.
    #
    # Out-File -Encoding UTF8 on BOTH branches, never Tee-Object: on Windows
    # PowerShell 5.1 Tee-Object -FilePath writes UTF-16LE and has no -Encoding
    # parameter, so teeing the run and appending the error as UTF-8 produced
    # one file in two encodings. Get-Content locked onto the UTF-16 BOM at the
    # head and rendered the error - the only part anyone needed to read - as
    # CJK mojibake.
    $inner = "try {{ & {0} -Role {1} -InstallDir {2} -SimulatorPort {3} *>&1 | Out-File -FilePath {4} -Encoding UTF8; exit 0 }} catch {{ `$_ | Out-String | Out-File -Append -Encoding UTF8 {4}; exit 1 }}" -f `
        (ConvertTo-PsLiteral $installScript),
        (Get-SelectedRole),
        (ConvertTo-PsLiteral $dir),
        $port,
        (ConvertTo-PsLiteral $logPath)

    try {
        $script:process = Start-Process powershell -Verb RunAs -PassThru -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $inner
        )
    } catch {
        # Declining the UAC prompt throws here. That is a choice, not a fault.
        $installButton.Enabled = $true
        $statusLabel.Text = "Cancelled - administrator rights are required to install."
        $statusLabel.ForeColor = [System.Drawing.Color]::Firebrick
        return
    }
    $timer.Start()
})

$openButton.Add_Click({
    $repo = Join-Path $dirText.Text.Trim() "app"
    $role = Get-SelectedRole
    $module = if ($role -eq "Simulator") { "simulator_gui.app" } else { "gui.app" }
    # No --config: each panel's own search path finds the file install.ps1
    # seeded, given the working directory. pythonw comes from the PATH entry
    # the bundled Python installer just added.
    # This window started before install.ps1 put Python on the PATH, and a
    # running process does not inherit later PATH changes - so pythonw.exe
    # cannot be resolved right after a first install. Re-read the machine
    # PATH, then fall back to the interpreter's usual location.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") +
                ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    # Under Set-StrictMode -Version Latest, reading `.Source` off a Get-Command
    # that returned $null throws, so the fallback below was dead code. Capture
    # the result first and only read the property when it exists.
    $pythonwCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    $pythonw = $null
    if ($pythonwCommand) { $pythonw = $pythonwCommand.Source }
    if (-not $pythonw) {
        $candidate = Get-ChildItem "C:\Program Files\Python*\pythonw.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($candidate) { $pythonw = $candidate.FullName }
    }
    if (-not $pythonw) { $pythonw = "pythonw.exe" }
    try {
        Start-Process $pythonw -ArgumentList "-m $module" -WorkingDirectory $repo
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            "Could not start the panel. Use the desktop shortcut instead.`n`n$_",
            "Could not open the panel", "OK", "Warning") | Out-Null
    }
})

$closeButton.Add_Click({ $form.Close() })

[void]$form.ShowDialog()
