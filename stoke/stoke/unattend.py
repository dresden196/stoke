"""Answer files for Windows Setup: the Rufus "Windows User Experience" options.

This is a port of Rufus's CreateUnattendXml(). Every option is a string in
a set rather than a bit in an int; the sections they land in are the same.
"""

import os
import re
import subprocess
from xml.sax.saxutils import escape

# Options, named as in Rufus (UNATTEND_*):
BYPASS_REQUIREMENTS = "bypass_requirements"      # SECUREBOOT_TPM_MINRAM
NO_ONLINE_ACCOUNT = "no_online_account"
NO_DATA_COLLECTION = "no_data_collection"
OFFLINE_INTERNAL_DRIVES = "offline_internal_drives"
DUPLICATE_LOCALE = "duplicate_locale"
SET_USER = "set_user"
DISABLE_BITLOCKER = "disable_bitlocker"
FORCE_S_MODE = "force_s_mode"
USE_MS2023_BOOTLOADERS = "use_ms2023_bootloaders"
APPLY_SKUSIPOLICY = "apply_skusipolicy"
SILENT_INSTALL = "silent_install"
QOL_ENHANCEMENTS = "qol_enhancements"
WINDOWS_TO_GO = "windows_to_go"

ALL_OPTIONS = (BYPASS_REQUIREMENTS, NO_ONLINE_ACCOUNT, NO_DATA_COLLECTION, OFFLINE_INTERNAL_DRIVES,
               DUPLICATE_LOCALE, SET_USER, DISABLE_BITLOCKER, FORCE_S_MODE, USE_MS2023_BOOTLOADERS,
               APPLY_SKUSIPOLICY, SILENT_INSTALL, QOL_ENHANCEMENTS)
DEFAULT_OPTIONS = {BYPASS_REQUIREMENTS, NO_ONLINE_ACCOUNT, OFFLINE_INTERNAL_DRIVES}

WINPE_SETUP = {BYPASS_REQUIREMENTS, SILENT_INSTALL}
SPECIALIZE_DEPLOYMENT = {NO_ONLINE_ACCOUNT, QOL_ENHANCEMENTS}
OOBE_SHELL_SETUP = {NO_DATA_COLLECTION, SET_USER, DUPLICATE_LOCALE, SILENT_INSTALL}
OOBE_INTERNATIONAL = {DUPLICATE_LOCALE}
OOBE = OOBE_SHELL_SETUP | OOBE_INTERNATIONAL | {DISABLE_BITLOCKER, USE_MS2023_BOOTLOADERS,
                                                APPLY_SKUSIPOLICY, QOL_ENHANCEMENTS}
OFFLINE_SERVICING = {OFFLINE_INTERNAL_DRIVES, FORCE_S_MODE}

BYPASS_NAMES = ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck")
ARCH_NAMES = {"x86": "x86", "x64": "amd64", "amd64": "amd64", "arm": "arm", "arm64": "arm64"}

UNALLOWED_ACCOUNT_NAMES = {n.lower() for n in (
    "Administrator", "Järjestelmänvalvoja", "Administrateur", "Rendszergazda", "Administrador",
    "Администратор", "Administratör", "Guest", "DefaultAccount", "WDAGUtilityAccount",
    "HelpAssistant", "KRBTGT", "Local", "NONE", "SYSTEM")}
USERNAME_INVALID_CHARS = '/\\[]:;|=.,+*?<>%@&"'
MAX_USERNAME_LENGTH = 128

COMPONENT = ('<component name="{name}" processorArchitecture="{arch}" language="neutral" '
             'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">')

# IANA zone -> Windows time zone name, for the common cases. Anything not
# listed simply leaves TimeZone out and Windows asks.
WINDOWS_TZ = {
    "UTC": "UTC", "Etc/UTC": "UTC", "Europe/London": "GMT Standard Time", "Europe/Dublin": "GMT Standard Time",
    "Europe/Lisbon": "GMT Standard Time", "Europe/Paris": "Romance Standard Time", "Europe/Brussels": "Romance Standard Time",
    "Europe/Madrid": "Romance Standard Time", "Europe/Berlin": "W. Europe Standard Time", "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Rome": "W. Europe Standard Time", "Europe/Vienna": "W. Europe Standard Time", "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time", "Europe/Oslo": "W. Europe Standard Time", "Europe/Copenhagen": "Romance Standard Time",
    "Europe/Prague": "Central Europe Standard Time", "Europe/Budapest": "Central Europe Standard Time", "Europe/Warsaw": "Central European Standard Time",
    "Europe/Athens": "GTB Standard Time", "Europe/Bucharest": "GTB Standard Time", "Europe/Helsinki": "FLE Standard Time", "Europe/Kyiv": "FLE Standard Time",
    "Europe/Kiev": "FLE Standard Time", "Europe/Istanbul": "Turkey Standard Time", "Europe/Moscow": "Russian Standard Time",
    "America/New_York": "Eastern Standard Time", "America/Detroit": "Eastern Standard Time", "America/Toronto": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time", "America/Winnipeg": "Central Standard Time", "America/Denver": "Mountain Standard Time",
    "America/Edmonton": "Mountain Standard Time", "America/Phoenix": "US Mountain Standard Time", "America/Los_Angeles": "Pacific Standard Time",
    "America/Vancouver": "Pacific Standard Time", "America/Anchorage": "Alaskan Standard Time", "Pacific/Honolulu": "Hawaiian Standard Time",
    "America/Halifax": "Atlantic Standard Time", "America/St_Johns": "Newfoundland Standard Time", "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Bogota": "SA Pacific Standard Time", "America/Lima": "SA Pacific Standard Time", "America/Caracas": "Venezuela Standard Time",
    "America/Santiago": "Pacific SA Standard Time", "America/Buenos_Aires": "Argentina Standard Time", "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time", "Africa/Cairo": "Egypt Standard Time", "Africa/Johannesburg": "South Africa Standard Time",
    "Africa/Lagos": "W. Central Africa Standard Time", "Africa/Nairobi": "E. Africa Standard Time", "Africa/Casablanca": "Morocco Standard Time",
    "Asia/Jerusalem": "Israel Standard Time", "Asia/Riyadh": "Arab Standard Time", "Asia/Dubai": "Arabian Standard Time", "Asia/Tehran": "Iran Standard Time",
    "Asia/Karachi": "Pakistan Standard Time", "Asia/Kolkata": "India Standard Time", "Asia/Calcutta": "India Standard Time", "Asia/Dhaka": "Bangladesh Standard Time",
    "Asia/Bangkok": "SE Asia Standard Time", "Asia/Jakarta": "SE Asia Standard Time", "Asia/Ho_Chi_Minh": "SE Asia Standard Time",
    "Asia/Singapore": "Singapore Standard Time", "Asia/Kuala_Lumpur": "Singapore Standard Time", "Asia/Manila": "Singapore Standard Time",
    "Asia/Shanghai": "China Standard Time", "Asia/Hong_Kong": "China Standard Time", "Asia/Taipei": "Taipei Standard Time", "Asia/Seoul": "Korea Standard Time",
    "Asia/Tokyo": "Tokyo Standard Time", "Australia/Perth": "W. Australia Standard Time", "Australia/Adelaide": "Cen. Australia Standard Time",
    "Australia/Darwin": "AUS Central Standard Time", "Australia/Brisbane": "E. Australia Standard Time", "Australia/Sydney": "AUS Eastern Standard Time",
    "Australia/Melbourne": "AUS Eastern Standard Time", "Australia/Hobart": "Tasmania Standard Time", "Pacific/Auckland": "New Zealand Standard Time",
}

# xkb layout -> Windows keyboard layout id, for InputLocale.
XKB_TO_KLID = {
    "us": "0409:00000409", "gb": "0809:00000809", "de": "0407:00000407", "fr": "040c:0000040c", "es": "0c0a:0000040a",
    "it": "0410:00000410", "pt": "0816:00000816", "br": "0416:00000416", "nl": "0413:00020409", "be": "080c:0000080c",
    "ch": "0807:00000807", "at": "0c07:00000407", "se": "041d:0000041d", "no": "0414:00000414", "dk": "0406:00000406",
    "fi": "040b:0000040b", "pl": "0415:00000415", "cz": "0405:00000405", "hu": "040e:0000040e", "ru": "0419:00000419",
    "ua": "0422:00000422", "tr": "041f:0000041f", "gr": "0408:00000408", "jp": "0411:00000411", "kr": "0412:00000412",
    "cn": "0804:00000804", "ca": "0c0c:00001009", "latam": "080a:0000080a", "ie": "1809:00001809", "in": "4009:00004009",
}


def host_locale():
    """(system_locale, user_locale, ui_language, input_locale, windows_tz) from this machine."""
    lang = os.environ.get("LC_ALL") or os.environ.get("LANG") or "en_US.UTF-8"
    m = re.match(r"([a-z]{2,3})(?:_([A-Z]{2}))?", lang)
    loc = f"{m.group(1)}-{m.group(2)}" if m and m.group(2) else (m.group(1) if m else "en-US")
    if "-" not in loc:
        loc = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "pt": "pt-BR",
               "ja": "ja-JP", "zh": "zh-CN", "ru": "ru-RU", "pl": "pl-PL", "nl": "nl-NL"}.get(loc, "en-US")
    layout = "us"
    try:
        r = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=5)
        m2 = re.search(r"X11 Layout:\s*(\S+)", r.stdout)
        if m2:
            layout = m2.group(1).split(",")[0]
    except Exception:
        pass
    klid = XKB_TO_KLID.get(layout, XKB_TO_KLID["us"])
    tz = None
    try:
        target = os.readlink("/etc/localtime")
        zone = target.split("zoneinfo/", 1)[1]
        tz = WINDOWS_TZ.get(zone)
    except (OSError, IndexError):
        pass
    return loc, loc, loc, klid, tz


def sanitize_username(name):
    """Returns (sanitized, warning or None). Empty string means: don't create one."""
    if not name:
        return "", None
    if name.lower() in UNALLOWED_ACCOUNT_NAMES:
        return "", f"'{name}' is not allowed as a local account name; option ignored"
    clean = "".join("_" if c in USERNAME_INVALID_CHARS else c for c in name)[:MAX_USERNAME_LENGTH].strip()
    warn = None if clean == name else "local account name contained invalid characters and was sanitized"
    return clean, warn


def build(arch, options, username="", edition_index=1, ui_language="en-US", log=None):
    """Return the unattend.xml text, or None when no option needs one.

    Also returns the (start, end) character positions of the RunSynchronous
    bypass section in the windowsPE pass, so it can be cut out again if the
    registry keys were applied directly to boot.wim.
    """
    options = set(options)
    if not options or arch not in ARCH_NAMES:
        return None, None
    xarch = ARCH_NAMES[arch]
    L = []
    removable = [None, None]

    def comp(name):
        return COMPONENT.format(name=name, arch=xarch)

    def say(msg):
        if log:
            log("• " + msg)

    L.append('<?xml version="1.0" encoding="utf-8"?>')
    L.append('<unattend xmlns="urn:schemas-microsoft-com:unattend">')

    if options & WINPE_SETUP:
        L.append('  <settings pass="windowsPE">')
        L.append("    " + comp("Microsoft-Windows-Setup"))
        L += ["      <UserData>", "        <AcceptEula>true</AcceptEula>", "        <ProductKey>",
              "          <Key />", "        </ProductKey>", "      </UserData>"]
        if SILENT_INSTALL in options:
            say("Silent install")
            L.append("      <DiskConfiguration>")
            L.append("        <WillShowUI>OnError</WillShowUI>")
            if DISABLE_BITLOCKER in options:
                L.append("        <DisableEncryptedDiskProvisioning>true</DisableEncryptedDiskProvisioning>")
            # Touching the install media's UEFI:NTFS partition label makes the
            # disk screen appear if the layout is not the simple one-disk case
            # (see Rufus #2960): a safety net against wiping the wrong disk.
            L += ['        <Disk wcm:action="modify">', "          <DiskID>1</DiskID>", "          <ModifyPartitions>",
                  '            <ModifyPartition wcm:action="modify">', "              <Order>1</Order>",
                  "              <PartitionID>2</PartitionID>", "              <Label>STOKE_BOOT</Label>",
                  "            </ModifyPartition>", "          </ModifyPartitions>", "        </Disk>",
                  '        <Disk wcm:action="add">', "          <DiskID>0</DiskID>", "          <WillWipeDisk>true</WillWipeDisk>",
                  "          <CreatePartitions>",
                  '            <CreatePartition wcm:action="add">', "              <Order>1</Order>", "              <Type>EFI</Type>",
                  "              <Size>260</Size>", "            </CreatePartition>",
                  '            <CreatePartition wcm:action="add">', "              <Order>2</Order>", "              <Type>MSR</Type>",
                  "              <Size>16</Size>", "            </CreatePartition>",
                  '            <CreatePartition wcm:action="add">', "              <Order>3</Order>", "              <Type>Primary</Type>",
                  "              <Extend>true</Extend>", "            </CreatePartition>",
                  "          </CreatePartitions>", "          <ModifyPartitions>",
                  '            <ModifyPartition wcm:action="add">', "              <Order>1</Order>", "              <PartitionID>1</PartitionID>",
                  "              <Label>EFI</Label>", "              <Format>FAT32</Format>", "            </ModifyPartition>",
                  '            <ModifyPartition wcm:action="add">', "              <Order>2</Order>", "              <PartitionID>3</PartitionID>",
                  "              <Label>Windows</Label>", "              <Letter>C</Letter>", "              <Format>NTFS</Format>",
                  "            </ModifyPartition>", "          </ModifyPartitions>", "        </Disk>", "      </DiskConfiguration>",
                  "      <ImageInstall>", "        <OSImage>", "          <WillShowUI>OnError</WillShowUI>", "          <InstallFrom>",
                  '            <MetaData wcm:action="add">', "              <Key>/IMAGE/INDEX</Key>",
                  f"              <Value>{edition_index}</Value>", "            </MetaData>", "          </InstallFrom>",
                  "          <InstallTo>", "            <DiskID>0</DiskID>", "            <PartitionID>3</PartitionID>",
                  "          </InstallTo>", "        </OSImage>", "      </ImageInstall>"]
        if BYPASS_REQUIREMENTS in options:
            say("Bypass Secure Boot / TPM / RAM requirements")
            removable[0] = len(L)
            L.append("      <RunSynchronous>")
            for i, name in enumerate(BYPASS_NAMES, 1):
                L += ['        <RunSynchronousCommand wcm:action="add">', f"          <Order>{i}</Order>",
                      f"          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {name} /t REG_DWORD /d 1 /f</Path>",
                      "        </RunSynchronousCommand>"]
            L.append("      </RunSynchronous>")
            removable[1] = len(L)
        L.append("    </component>")
        if SILENT_INSTALL in options:
            L.append("    " + comp("Microsoft-Windows-International-Core-WinPE"))
            L.append(f"      <UILanguage>{escape(ui_language)}</UILanguage>")
            L.append("    </component>")
        L.append("  </settings>")

    if options & SPECIALIZE_DEPLOYMENT:
        cmds = []
        if NO_ONLINE_ACCOUNT in options:
            say("Bypass online account requirement")
            cmds.append('reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f')
        if QOL_ENHANCEMENTS in options:
            say("QoL: disable OneDrive and Outlook by default")
            cmds += [
                'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\OneDrive" /v DisableFileSyncNGSC /t REG_DWORD /d 1 /f',
                'PowerShell -NonInteractive -WindowStyle Hidden -Command "Remove-Item -Path $env:SystemRoot\\System32\\OneDriveSetup.exe -Force -Confirm:$false; Remove-Item -Path $env:SystemRoot\\SysWOW64\\OneDriveSetup.exe -Force -Confirm:$false;"',
                'PowerShell -NonInteractive -WindowStyle Hidden -Command "Get-AppxProvisionedPackage -Online | Where-Object {$_.PackageName -like \'*Outlook*\'} | Remove-AppxProvisionedPackage -Online"',
                'PowerShell -NonInteractive -WindowStyle Hidden -Command "Get-AppxPackage -AllUsers *Outlook* | Remove-AppxPackage -AllUsers"',
                'PowerShell -NonInteractive -WindowStyle Hidden -Command "Get-AppxProvisionedPackage -Online | Where-Object {$_.PackageName -like \'*Teams*\'} | Remove-AppxProvisionedPackage -Online"',
                'PowerShell -NonInteractive -WindowStyle Hidden -Command "Get-AppxPackage -AllUsers *Teams* | Remove-AppxPackage -AllUsers"',
            ]
        L.append('  <settings pass="specialize">')
        L.append("    " + comp("Microsoft-Windows-Deployment"))
        if cmds:
            L.append("      <RunSynchronous>")
            for i, c in enumerate(cmds, 1):
                L += ['        <RunSynchronousCommand wcm:action="add">', f"          <Order>{i}</Order>",
                      f"          <Path>{escape(c)}</Path>", "        </RunSynchronousCommand>"]
            L.append("      </RunSynchronous>")
        L.append("    </component>")
        L.append("  </settings>")

    if options & OOBE:
        L.append('  <settings pass="oobeSystem">')
        if options & OOBE_SHELL_SETUP:
            L.append("    " + comp("Microsoft-Windows-Shell-Setup"))
            cmds = []
            if options & {NO_DATA_COLLECTION, SILENT_INSTALL}:
                say("Disable data collection")
                L += ["      <OOBE>", "        <HideEULAPage>true</HideEULAPage>", "        <ProtectYourPC>3</ProtectYourPC>"]
                if SILENT_INSTALL in options:
                    L += ["        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>",
                          "        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>"]
                L.append("      </OOBE>")
            sys_loc, user_loc, ui_loc, klid, tz = host_locale()
            if DUPLICATE_LOCALE in options and tz:
                L.append(f"      <TimeZone>{escape(tz)}</TimeZone>")
            if SET_USER in options:
                clean, warn = sanitize_username(username)
                if warn and log:
                    log("WARNING: " + warn)
                if clean:
                    say(f"Use '{clean}' for local account name")
                    L += ["      <UserAccounts>", "        <LocalAccounts>", '          <LocalAccount wcm:action="add">',
                          f"            <Name>{escape(clean)}</Name>", f"            <DisplayName>{escape(clean)}</DisplayName>",
                          "            <Group>Administrators;Power Users</Group>",
                          "            <Password>", "              <Value>UABhAHMAcwB3AG8AcgBkAA==</Value>",
                          "              <PlainText>false</PlainText>", "            </Password>",
                          "          </LocalAccount>", "        </LocalAccounts>", "      </UserAccounts>"]
                    cmds.append(f'net user "{clean}" /logonpasswordchg:yes')
                    cmds.append("net accounts /maxpwage:unlimited")
            if APPLY_SKUSIPOLICY in options:
                say("Apply SkuSiPolicy.p7b")
                cmds.append("cmd /c mountvol S: /S && copy %WINDIR%\\system32\\SecureBootUpdates\\SkuSiPolicy.p7b "
                            "S:\\EFI\\Microsoft\\Boot && mountvol S: /D")
            if QOL_ENHANCEMENTS in options:
                say("QoL: disable Fast Startup, Copilot, recommendations, news and Teams by default")
                cmds += [
                    'reg add "HKLM\\System\\CurrentControlSet\\Control\\Session Manager\\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f',
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowCopilotButton /t REG_DWORD /d 0 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\WindowsCopilot" /v TurnOffWindowsCopilot /t REG_DWORD /d 1 /f',
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Search" /v SearchboxTaskbarMode /t REG_DWORD /d 1 /f',
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Search" /v SearchboxTaskbarModeCache /t REG_DWORD /d 1 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v DisableWindowsConsumerFeatures /t REG_DWORD /d 1 /f',
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager" /v SystemPaneSuggestionsEnabled /t REG_DWORD /d 0 /f',
                    'reg add "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search" /v BingSearchEnabled /t REG_DWORD /d 0 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\Device Metadata" /v PreventDeviceMetadataFromNetwork /t REG_DWORD /d 1 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Dsh" /v AllowNewsAndInterests /t REG_DWORD /d 0 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\Windows Feeds" /v EnableFeeds /t REG_DWORD /d 0 /f',
                    'reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Communications" /v ConfigureChatAutoInstall /t REG_DWORD /d 0 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v DisableCloudOptimizedContent /t REG_DWORD /d 1 /f',
                    'reg add "HKLM\\Software\\Policies\\Microsoft\\Edge" /v HideFirstRunExperience /t REG_DWORD /d 1 /f',
                    'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v Start_Layout /t REG_DWORD /d 1 /f',
                    "PowerShell -NonInteractive -WindowStyle Hidden -Command \"Set-ItemProperty -Path 'Registry::HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Start' "
                    "-Name 'VisiblePlaces' -Value $([convert]::FromBase64String('ztU0LVr6Q0WC8iLm6vd3PC+zZ+PeiVVDv85h83sYqTe8JIo"
                    "UDNaJQqCAbtm7okiCRIF1/g0IrkKL2jTtl7ZjlEqwvXRK+WhPi9ZDmAcdqLyGCHNSqlFDQp97J3ZYRlnU')) -Type 'Binary'\"",
                    'reg add "HKCU\\Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32" /ve /t REG_SZ /d "" /f',
                ]
            if cmds:
                L.append("      <FirstLogonCommands>")
                for i, c in enumerate(cmds, 1):
                    L += ['        <SynchronousCommand wcm:action="add">', f"          <Order>{i}</Order>",
                          f"          <CommandLine>{escape(c)}</CommandLine>", "        </SynchronousCommand>"]
                L.append("      </FirstLogonCommands>")
            L.append("    </component>")
        if options & OOBE_INTERNATIONAL:
            say("Use the same regional options as this machine")
            sys_loc, user_loc, ui_loc, klid, tz = host_locale()
            L.append("    " + comp("Microsoft-Windows-International-Core"))
            L += [f"      <InputLocale>{klid}</InputLocale>", f"      <SystemLocale>{sys_loc}</SystemLocale>",
                  f"      <UserLocale>{user_loc}</UserLocale>", f"      <UILanguage>{ui_loc}</UILanguage>",
                  f"      <UILanguageFallback>en-US</UILanguageFallback>"]
            L.append("    </component>")
        if DISABLE_BITLOCKER in options:
            say("Disable BitLocker")
            L.append("    " + comp("Microsoft-Windows-SecureStartup-FilterDriver"))
            L.append("      <PreventDeviceEncryption>true</PreventDeviceEncryption>")
            L.append("    </component>")
            L.append("    " + comp("Microsoft-Windows-EnhancedStorage-Adm"))
            L.append("      <TCGSecurityActivationDisabled>1</TCGSecurityActivationDisabled>")
            L.append("    </component>")
        L.append("  </settings>")

    if options & OFFLINE_SERVICING:
        L.append('  <settings pass="offlineServicing">')
        if OFFLINE_INTERNAL_DRIVES in options:
            say("Set internal drives offline")
            L.append("    " + comp("Microsoft-Windows-PartitionManager"))
            L.append("      <SanPolicy>4</SanPolicy>")
            L.append("    </component>")
        if FORCE_S_MODE in options:
            say("Enforce S Mode")
            L.append("    " + comp("Microsoft-Windows-CodeIntegrity"))
            L.append("      <SkuPolicyRequired>1</SkuPolicyRequired>")
            L.append("    </component>")
        L.append("  </settings>")

    if USE_MS2023_BOOTLOADERS in options:
        say("Use 'Windows UEFI CA 2023' signed bootloaders")

    L.append("</unattend>")
    return L, tuple(removable)


def render(lines):
    return "\n".join(lines) + "\n"


def without_bypass_section(lines, removable, only_bypass):
    """After the LabConfig keys were written straight into boot.wim, the
    RunSynchronous fallback is not needed: drop it, or disable the whole
    windowsPE pass if that was all it held."""
    lines = list(lines)
    if only_bypass:
        return [l.replace('<settings pass="windowsPE">', '<settings pass="disabled">') for l in lines]
    a, b = removable
    if a is not None and b is not None:
        del lines[a:b]
    return lines
