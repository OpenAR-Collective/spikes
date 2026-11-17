# Every 30 seconds, each Windows process's CPU seconds and bytes written over
# the interval, for a measured pass that asks for it (process_snapshot in its
# plan). The runner starts it and stops it; it also ends itself at -Until, so
# it cannot outlive a runner that dies. The output names every process on the
# host, so it goes to the harness's ignored out/ folder and is never
# committed as it is; a committed copy shows only the bake-off's own
# processes by name.
#
# Each row is one process over one interval: the interval's end in UTC, its
# length in seconds, the process ID, its parent's ID, its name, the CPU
# seconds it used (kernel and user), the bytes it wrote (Windows' count of
# write operations, which takes in file, network, and device writes alike),
# and whether it is the runner or one of its descendants (1 or 0), judged in
# that snapshot from the live process tree, with each child created after its
# parent, so that a process ID Windows has reused is never taken for one.
# The virtual machine Docker runs in shows its CPU but not its disk writes,
# which Windows does not charge to it. A row is written for a process that
# used at least 0.05 CPU seconds or wrote at least 64 KB in the interval, or
# that is the runner or one of its descendants; one row named _all sums every
# process seen at both ends of the interval; and one named _snapshot gives, as
# its CPU seconds, how long the snapshot's own query took, in wall-clock
# seconds. A process that starts and ends within one interval is not seen.
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][datetime]$Until,
    [int]$Runner = 0,
    [int]$Every = 30
)
$ErrorActionPreference = 'Stop'
$invariant = [Globalization.CultureInfo]::InvariantCulture
$writer = New-Object IO.StreamWriter($Out, $true, (New-Object Text.UTF8Encoding($false)))
$writer.WriteLine("utc,interval_s,pid,parent_pid,name,cpu_s,write_bytes,ours")
$writer.Flush()
$previous = $null
$previousAt = $null
$start = Get-Date
$tick = 0
try {
    while ((Get-Date) -lt $Until) {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $at = [DateTime]::UtcNow
        $now = @{}
        $byPid = @{}
        foreach ($p in Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name, KernelModeTime, UserModeTime, WriteTransferCount, CreationDate) {
            $entry = @($p.ProcessId, $p.ParentProcessId, $p.Name,
                [double]$p.KernelModeTime + [double]$p.UserModeTime, [double]$p.WriteTransferCount, $p.CreationDate)
            $now["$($p.ProcessId)|$($p.Name)"] = $entry
            $byPid[[int]$p.ProcessId] = $entry
        }
        $queried = $clock.Elapsed.TotalSeconds
        if ($null -ne $previous) {
            $interval = ($at - $previousAt).TotalSeconds
            $stamp = $at.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", $invariant)
            # The runner and its descendants, by parent, from this snapshot.
            $ours = @{}
            if ($Runner -gt 0) {
                $ours[$Runner] = $true
                do {
                    $grew = $false
                    foreach ($v in $now.Values) {
                        $parent = $byPid[[int]$v[1]]
                        if ($ours.ContainsKey([int]$v[1]) -and -not $ours.ContainsKey([int]$v[0]) -and $null -ne $parent -and
                            $null -ne $v[5] -and $null -ne $parent[5] -and $v[5] -ge $parent[5]) {
                            $ours[[int]$v[0]] = $true
                            $grew = $true
                        }
                    }
                } while ($grew)
            }
            $allCpu = 0.0
            $allWrite = 0.0
            foreach ($key in $now.Keys) {
                if (-not $previous.ContainsKey($key)) { continue }
                $v = $now[$key]
                # Process 0 is the idle process: its CPU time is idle time.
                if ([int]$v[0] -eq 0) { continue }
                $cpu = ($v[3] - $previous[$key][3]) / 1e7
                $write = $v[4] - $previous[$key][4]
                if ($cpu -lt 0 -or $write -lt 0) { continue }
                $allCpu += $cpu
                $allWrite += $write
                $mine = $ours.ContainsKey([int]$v[0])
                if ($cpu -ge 0.05 -or $write -ge 65536 -or $mine) {
                    $name = $v[2] -replace ',', ' '
                    $writer.WriteLine([string]::Format($invariant, "{0},{1:F1},{2},{3},{4},{5:F3},{6:F0},{7}", $stamp, $interval,
                        $v[0], $v[1], $name, $cpu, $write, $(if ($mine) { 1 } else { 0 })))
                }
            }
            $writer.WriteLine([string]::Format($invariant, "{0},{1:F1},0,0,_all,{2:F3},{3:F0},0", $stamp, $interval, $allCpu, $allWrite))
            $writer.WriteLine([string]::Format($invariant, "{0},{1:F1},0,0,_snapshot,{2:F3},0,0", $stamp, $interval, $queried))
            $writer.Flush()
        }
        $previous = $now
        $previousAt = $at
        $tick += 1
        $next = $start.AddSeconds($tick * $Every)
        $wait = ($next - (Get-Date)).TotalMilliseconds
        if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]$wait) }
    }
}
finally {
    $writer.Close()
}
