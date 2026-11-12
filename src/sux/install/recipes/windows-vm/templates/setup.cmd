@echo off
rem $header
rem Runs once, elevated, at the first logon after Windows Setup.

rem Remote Desktop sessions render on the hardware GPU and encode with it.
set TS="HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
reg add %TS% /v bEnumerateHWBeforeSW /t REG_DWORD /d 1 /f
reg add %TS% /v AVCHardwareEncodePreferred /t REG_DWORD /d 1 /f
reg add %TS% /v AVC444ModePreferred /t REG_DWORD /d 1 /f

rem A VM that sleeps cannot be reached.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

rem Last, because the host takes the guest agent's first answer as "Setup is
rem done": drivers, the SPICE agent and the QEMU guest agent from the virtio CD.
for %%d in (D E F G H I) do if exist %%d:\virtio-win-guest-tools.exe start /wait %%d:\virtio-win-guest-tools.exe /install /passive /norestart
