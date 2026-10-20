"""Windows Task Scheduler definitions (XML) for the daily run, weekly review and the bot."""
from typing import Literal
from xml.sax.saxutils import escape


def _split(command: str) -> tuple[str, str]:
    if command.startswith('"'):
        exe, _, rest = command[1:].partition('"')
        return exe, rest.strip()
    exe, _, rest = command.partition(" ")
    return exe, rest.strip()


def task_xml(name: str, command: str, trigger: Literal["daily", "weekly", "logon"], time: str | None) -> str:
    exe, args = _split(command)
    if trigger == "logon":
        trig = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
    else:
        start = f"2026-01-01T{time}:00"
        sched = ("<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>" if trigger == "daily" else
                 "<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Sunday /></DaysOfWeek></ScheduleByWeek>")
        trig = f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>{sched}</CalendarTrigger>"
    limit = "PT0S" if trigger == "logon" else "PT2H"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(name)}</Description></RegistrationInfo>
  <Triggers>{trig}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""
