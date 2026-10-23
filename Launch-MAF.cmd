@echo off
setlocal
set "MAF_LAUNCHER=%~f0"
set "MAF_ACTION=%~1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create(([regex]::Split([IO.File]::ReadAllText($env:MAF_LAUNCHER), '(?m)^# POWERSHELL\r?$'))[1]))"
set "MAF_EXIT=%ERRORLEVEL%"
if "%MAF_ACTION%"=="" if not "%MAF_EXIT%"=="0" pause
exit /b %MAF_EXIT%
# POWERSHELL
$ErrorActionPreference = 'Stop'

function Get-MafListeners {
    $ports = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq 8080 })
    if (!$ports.Count) { return }
    if (@($ports | Where-Object { $_.Address.ToString() -ne '127.0.0.1' }).Count) {
        throw 'Port 8080 is not bound exclusively to 127.0.0.1. No process was changed.'
    }
    Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction Stop
}

function Test-MafCommandLine($Process, [string]$Executable, [string]$Workflow) {
    if (!$Process.ExecutablePath -or !$Process.CommandLine -or !$Process.CreationDate) { return $false }
    if (![string]::Equals([IO.Path]::GetFullPath($Process.ExecutablePath), $Executable, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    $exe = [regex]::Escape($Executable)
    $file = [regex]::Escape($Workflow)
    $pattern = '^\s*(?:"' + $exe + '"|' + $exe + ')(?:\s+-(?:B|u)|\s+-X\s+utf8)*\s+(?:"' + $file + '"|' + $file + '|-m\s+application\.council\.workflow)\s*$'
    return $Process.CommandLine -match $pattern
}

function Get-MafBackend {
    $listeners = @(Get-MafListeners)
    if (!$listeners.Count) { return }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'Ambiguous port 8080 ownership; no process was changed.' }
    $root = Split-Path -Parent $env:MAF_LAUNCHER
    $python = [IO.Path]::GetFullPath((Join-Path $root 'runtime\python\.venv\Scripts\python.exe'))
    $workflow = [IO.Path]::GetFullPath((Join-Path $root 'application\council\workflow.py'))
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listeners[0].OwningProcess) -ErrorAction Stop
    if (!$process) { throw 'Port ownership changed before identification; nothing was stopped or started.' }
    $wrapper = $null
    if (!(Test-MafCommandLine $process $python $workflow)) {
        $venvHomeLine = @(Get-Content -LiteralPath (Join-Path $root 'runtime\python\.venv\pyvenv.cfg') | Where-Object { $_ -match '^home\s*=' })
        if ($venvHomeLine.Count -ne 1) { throw 'Cannot establish the virtual environment base interpreter.' }
        $basePython = [IO.Path]::GetFullPath((Join-Path ($venvHomeLine[0].Split('=',2)[1].Trim()) 'python.exe'))
        $wrapper = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $process.ParentProcessId) -ErrorAction Stop
        if (!(Test-MafCommandLine $process $basePython $workflow) -or
            !(Test-MafCommandLine $wrapper $python $workflow) -or $wrapper.CreationDate -gt $process.CreationDate) {
            throw ('Port 8080 PID ' + $process.ProcessId + ' is not a verified project Council. No process was changed.')
        }
    }
    [pscustomobject]@{Id=$process.ProcessId; Created=$process.CreationDate; WrapperId=if ($wrapper) {$wrapper.ProcessId} else {$null}}
}

function Stop-Maf {
    $server = Get-MafBackend
    if (!$server) { Write-Host 'STOPPED: port 8080 is already free.'; return }
    # Pin a handle, then repeat identity/creation-time checks before killing one process.
    $handle = Get-Process -Id $server.Id -ErrorAction Stop
    try {
        $null = $handle.Handle
        $current = Get-MafBackend
        if (!$current -or $current.Id -ne $server.Id -or $current.Created -ne $server.Created -or
            [Math]::Abs(($handle.StartTime - $server.Created).TotalMilliseconds) -gt 1) {
            throw 'Process identity changed during shutdown checks; nothing was terminated.'
        }
        $handle.Kill() # One verified process only; never a process tree or a name match.
        if (!$handle.WaitForExit(5000)) { throw 'The verified MAF process has not exited; shutdown did not complete.' }
    }
    finally { $handle.Dispose() }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while (@(Get-MafListeners).Count -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 200 }
    if (@(Get-MafListeners).Count) { throw 'MAF exited, but port 8080 is still occupied. No other process was touched.' }
    Write-Host ('STOPPED: verified MAF PID ' + $server.Id + ', created ' + $server.Created.ToString('o') + '; port 8080 is free.')
}

function Get-MafJson([string]$Path) {
    # Local metadata only: no redirects, proxies, model execution, or token in a URL.
    $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:8080' + $Path)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 2000
    $request.ReadWriteTimeout = 2000
    $request.Headers['Authorization'] = 'Bearer ' + $env:DEVUI_AUTH_TOKEN
    $response = $request.GetResponse()
    try {
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        try { $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
    }
    finally { $response.Dispose() }
}

function Start-Maf([switch]$NoBrowser) {
    # Refresh only the User variables consumed by this application; keep the system PATH.
    $token = [Environment]::GetEnvironmentVariable('DEVUI_AUTH_TOKEN', 'User')
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'DEVUI_AUTH_TOKEN is missing from Windows User environment variables. Save it there before launching; no token was generated.'
    }
    $env:DEVUI_AUTH_TOKEN = $token
    foreach ($name in @('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY')) {
        $value = [Environment]::GetEnvironmentVariable($name, 'User')
        if ($null -ne $value) { [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
            throw ('Missing ' + $name + '; no backend or browser was started.')
        }
    }

    $root = Split-Path -Parent $env:MAF_LAUNCHER
    $python = Join-Path $root 'runtime\python\.venv\Scripts\python.exe'
    $workflow = Join-Path $root 'application\council\workflow.py'
    if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $workflow -PathType Leaf)) {
        throw 'The existing Python environment or Council entry point is missing.'
    }
    if (!$NoBrowser) {
      $brave = @(
        "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
        "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe",
        "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (!$brave) { throw 'Brave is not installed in a standard location; nothing was started.' }
    $userData = Join-Path $env:LOCALAPPDATA 'BraveSoftware\Brave-Browser\User Data'
    try { $state = Get-Content -LiteralPath (Join-Path $userData 'Local State') -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'Cannot read the existing Brave profile metadata; nothing was started.' }
    $profile = [string]$state.profile.last_used
    if (!$profile) { $profile = [string]@($state.profile.last_active_profiles)[0] }
    if (!$profile) { $profile = 'Default' }
    if ($profile -notmatch '^(Default|Profile [0-9]+)$' -or !(Test-Path -LiteralPath (Join-Path $userData $profile) -PathType Container)) {
        throw 'Cannot identify an existing Brave profile; no new profile will be created.'
    }
    }
    $profiles = Get-Content -LiteralPath (Join-Path $root 'configuration\council\profiles.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $expected = @(
        $profiles.testing.name
        $profiles.conversation.name
        $profiles.frontier.name
    )

    $server = Get-MafBackend
    $backend = $null
    if (!$server) {
        $backend = Start-Process -FilePath $python -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -ArgumentList @('-B', '-u', '-X', 'utf8', ('"{0}"' -f $workflow))
        Write-Host ('Starting MAF through virtual-environment PID ' + $backend.Id + '. No models are run at startup.')
    }
    else { Write-Host ('Existing verified MAF PID ' + $server.Id + '; checking authenticated readiness.') }

    $ready = $false
    $lastFailure = 'No verified MAF listener became reachable.'
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        if ($backend -and $backend.HasExited) { throw ('MAF exited before becoming ready. Last check: ' + $lastFailure + ' No browser was opened.') }
        $current = Get-MafBackend
        if ($server -and (!$current -or $current.Id -ne $server.Id -or $current.Created -ne $server.Created)) {
            throw 'The existing MAF process changed during readiness checks; no browser was opened.'
        }
        if ($backend -and $current -and $current.Id -ne $backend.Id -and $current.WrapperId -ne $backend.Id) {
            throw 'A different process took port 8080 during startup; it was left untouched.'
        }
        if (!$current) { Start-Sleep -Milliseconds 500; continue }
        $lastFailure = 'DevUI metadata could not be read (connection, authentication, or response format).'
        try {
            $meta = Get-MafJson '/meta'
            $listing = Get-MafJson '/v1/entities'
            $names = @($listing.entities | ForEach-Object { $_.name })
            if ($meta.auth_required -ne $true) {
                $lastFailure = 'DevUI did not report authentication enabled.'
                throw $lastFailure
            }
            if ($names.Count -ne $expected.Count -or @(Compare-Object $expected $names -CaseSensitive).Count) {
                $lastFailure = 'Workflow registration mismatch: expected ' + $expected.Count + '; observed ' + $names.Count + '. Required names: ' + ($expected -join '; ') + '.'
                throw $lastFailure
            }
            if (@($listing.entities | Where-Object { $_.type -ne 'workflow' -or $_.framework -ne 'agent_framework' }).Count) {
                $lastFailure = 'DevUI entities must all be agent_framework workflows.'
                throw $lastFailure
            }
            $verified = Get-MafBackend
            if (!$verified -or $verified.Id -ne $current.Id -or $verified.Created -ne $current.Created) {
                $lastFailure = 'MAF ownership changed during the readiness request.'
                throw $lastFailure
            }
            $ready = $true
            break
        }
        catch {
            # Keep only controlled diagnostics, never HTTP bodies, headers, or raw exception messages.
            $httpFailure = $_.Exception
            while ($httpFailure.InnerException -and $httpFailure -isnot [Net.WebException]) { $httpFailure = $httpFailure.InnerException }
            if ($httpFailure -is [Net.WebException]) {
                if ($httpFailure.Response) {
                    $lastFailure = 'DevUI metadata request failed (HTTP ' + [int]$httpFailure.Response.StatusCode + ').'
                }
                else { $lastFailure = 'DevUI metadata request failed (' + $httpFailure.Status + ').' }
            }
            if (!$backend) {
                throw ('The verified MAF server did not pass readiness checks. Last check: ' + $lastFailure + ' It was left untouched.')
            }
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    if (!$ready) { throw ('MAF did not become ready within 45 seconds. Last check: ' + $lastFailure + ' No browser was opened; no process was terminated.') }
    $action = if ($backend) { 'STARTED' } else { 'REUSED' }
    if (!$backend) { Write-Host 'REUSED: backend retains credentials and settings loaded at startup; saved changes require a deliberate restart.' }
    Write-Host ($action + ': verified MAF PID ' + $current.Id + ', created ' + $current.Created.ToString('o') + '. READY: http://127.0.0.1:8080; authentication and ' + $expected.Count + ' Councils verified: ' + ($expected -join '; ') + '.')
    if (!$NoBrowser) { Start-Process -FilePath $brave -ArgumentList @(('--profile-directory="' + $profile + '"'), 'http://127.0.0.1:8080') | Out-Null }
}

$mutex = [Threading.Mutex]::new($false, 'Local\MultiModelCouncil-8080')
$locked = $false
try {
    $launchAction = [string]$env:MAF_ACTION
    if ($launchAction -notin @('', '--stop', '--no-browser')) { throw 'Supported options: --stop or --no-browser.' }
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) { throw 'Another MAF launcher/shutdown is already running; no second backend was started.' }
    if ($launchAction -eq '--stop') { Stop-Maf }
    else { Start-Maf -NoBrowser:($launchAction -eq '--no-browser') }
}
catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }
finally { if ($locked) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
