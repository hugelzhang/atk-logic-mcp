# Elevated USBPcap capture session (ASCII only - Windows PowerShell reads .ps1 as ANSI
# on zh-CN systems, so non-ASCII text can break parsing).
# Behaviour: init xHCI NonStandardHWIDs -> capture USBPcap1..4 -> wait for STOP flag -> stop all
$ErrorActionPreference = 'Continue'
$dir  = 'D:\MCP\01-atk-logic\atk-logic\tools\usbcap'
$log  = "$dir\capture_session.log"
$flag = "$dir\STOP"
$exe  = 'C:\Program Files\USBPcap\USBPcapCMD.exe'

function Log($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Out-File -FilePath $log -Append -Encoding utf8 }

"=== capture session start ($([DateTime]::Now)) ===" | Out-File -FilePath $log -Encoding utf8
Remove-Item $flag -ErrorAction SilentlyContinue
Get-ChildItem "$dir\hub*.pcap" -ErrorAction SilentlyContinue | Remove-Item -ErrorAction SilentlyContinue

Log "step1: init NonStandardHWIDs (-I)"
try { & $exe -I 2>&1 | Out-File -FilePath $log -Append -Encoding utf8 } catch { Log "  -I failed: $_" }

Log "step2: start capture on hubs 1..4"
$procs = @()
foreach ($n in 1..4) {
    $out = "$dir\hub$n.pcap"
    try {
        $p = Start-Process -FilePath $exe -ArgumentList @('-d', "\\.\USBPcap$n", '-A', '-o', $out) -PassThru `
             -RedirectStandardOutput "$dir\hub$n.out.txt" -RedirectStandardError "$dir\hub$n.err.txt"
        $procs += [pscustomobject]@{ Hub = $n; Pid = $p.Id; File = $out }
        Log "  USBPcap$n -> pid $($p.Id)"
    } catch {
        Log "  USBPcap$n failed to start: $_"
    }
}
Start-Sleep -Seconds 3
foreach ($x in $procs) {
    $sz = if (Test-Path $x.File) { (Get-Item $x.File).Length } else { -1 }
    Log "  hub$($x.Hub).pcap = $sz bytes (greater than 24 means traffic)"
}

$t0 = Get-Date
while (-not (Test-Path $flag)) {
    Start-Sleep -Seconds 1
    if (((Get-Date) - $t0).TotalSeconds -gt 900) { Log "timeout 900s, auto stop"; break }
}
Log "step3: STOP flag seen, waiting 3s to flush"
Start-Sleep -Seconds 3
foreach ($x in $procs) { Stop-Process -Id $x.Pid -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
foreach ($x in $procs) {
    $sz = if (Test-Path $x.File) { (Get-Item $x.File).Length } else { -1 }
    Log "  final hub$($x.Hub).pcap = $sz bytes"
}
Log "=== done ==="
