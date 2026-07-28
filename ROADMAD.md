ROADMAD v0.1.0 - v0.1.5
fixes and improvements
---------------------------------------

# CREATE A NEW SERVER

# RECOGNIZE VERSION (PAPER AND VANILLA) AND BUILD (PAPER-ONLY, displayed alongside ver (ex: 1.20-60, 26.2-45))

## VERSION DETECTION

The detector should determine the version once and cache the result as instance metadata. Detection should only be repeated when necessary (for example, if the configured server JAR changes or the user explicitly refreshes the version).

### Priority 1 — Known Version (Preferred)

If the instance was created through **Create New Server**, the application already knows the selected version/build from the download workflow.

Store this information as the instance's version metadata. This is the authoritative version unless the server software is later changed.

### Priority 2 — Live Detection

If the instance was imported through **Choose Existing Server**, attempt to determine the version by querying the running server.

Use the server's own reported version rather than inferring it from filenames.

If successful, store the detected version/build as the instance's version metadata.

This becomes the authoritative version for subsequent launches and UI displays.

### Priority 3 — JAR Inspection (Fallback)

If no version metadata exists and the server cannot be queried (for example, because it has never been started or is currently offline), inspect the configured server JAR.

Read the version/build information from the JAR metadata rather than relying on the filename.

If successful, store the detected version/build as the instance's version metadata.

### Version Refresh

Once a version has been successfully detected, the application should continue using the stored metadata instead of performing detection on every startup.

The detector should only run again when one of the following occurs:

* The configured server JAR was changed manually, outside the MineManager standard Update version/buid flow.
* The user explicitly requests a version refresh.
* Existing version metadata is missing or invalid.

This avoids unnecessary filesystem inspection and server queries while ensuring the stored version remains accurate.

## Display

Display the detected version in the Instance Viewer.

Examples:

* Vanilla: `1.10.2`, `26.2`
* Paper: `1.10.2 #60`,`26.2 #60` (meaning Paper version "26.2" and build 60)`
* Velocity: `3.5.1`

The exact formatting may be adjusted for readability, but it should always expose the useful version/build information.

## Notes
The detector should expose a normalized result containing, at minimum:

* Software type (already known by the application)
* Version
* Build (when applicable)
* Detection source (`Known`, `LiveServer`, or `Jar`)

Remove the **STATE** field from the Instance Viewer (when inspecting an instance) and replace it with **VERSION**.
With the state removed, indicate the state of the instance by coloring it accordinly, while maintaining desing.

The displayed version must represent the actual server version and, when applicable, its build (for example, Paper build). The detector must support **Vanilla**, **Paper**, and **Velocity** instances. The instance type is already known by the application through its mandatory software tag, so the detector only needs to determine the version/build information.


# D) SETTINGS UPDATE [VERSION DEPENDENT?]

Add dedicated endpoint and flow to update/overwrite server-icon 

bonus: feat: render img files (png, jpg, jpeg) in file explorer

1-Velocity proxy ON/OFF switch: Feature only in Paper instances. In settings, add a Velocity ON/OFF switch. Beside it, a Velocity online-mode switch (grayed-out if velocity switch is disabled) and a field in the for the fowarding.secret in setting > secrets.

essentialy, those 3 vars (fowarding secret, velocity enabled bool and online-mode bool) will read and write from these options in "<server_root>/config/paper-global.yml":

"[...]
proxies:
 bungee-cord:
   online-mode: true
 proxy-protocol: false
 velocity:
   enabled: true
   online-mode: false
   secret: vtge6t7wd8h4

[...]"

They must always reflect the state of the file, there shouldn't be a way for the Settings and the actual file to be desynced.

Always on switch in settings (starts the server automatically when the agent boots up -- so servers dont need manual start after reboots) 

save below to properties?
2-Add a online-mode ON/OFF switch (ON= premium only, OFF= allow cracked) in the instance Settings that controls "online-mode" bool in server.propeties. It must always reflect the state of the file, there shouldn't be a way for the Settings and the actual file to be desynced.

3-Add world type in settings so user can change worldtype (flat, normal,). If changed, show a little warning that they might have to delete their current world for the config to make effect.

4-Add RAM slider (we have it while Adding a new instance) into settings, using our already written logic to keep the -Xmx arg in sync with the slider

5-remove the hover effect on the slider
-----------------------------------------
BUGS:

2) When you change from instance A > File to Instance B > File, while viewing instance's A file exporer (with a opened file in the editor), the file opened in the editor doesn't change. Ideally we should close the current file being presented when changing instances.

3) there is a noticable delay between loading the website and it acquiring the state the server is in. The node state does not have this delay. Identify the issue and see if it's a core unfortunate feature our chosen architecture brought or a quick fix
2.1) In startup, stopped servers (those with Always on turned OFF) are "unknown" instead of stopped

Console quick fixes:

4) Log power actions in console and add more spacing to separate visually different sessions.
4.1) Log tmux erros, sessions and results more reliably

5) If a instance is stopped and clicked on, do not populate/fetch logs from latest, show the console empty.

Finally, quick question: Is gracefull stop servers if systemctl restart/stop or machine reboot possible? Afraid to corrupt world or serverfiles, our MineManager should be safe. 


Restart button reset console history in UI (MINEMANAGER OPTION COFIG)


# NOTES
add instance runtime
handle diff java versions for diff mc versions?

add node available binds?
add node settings

show port being used instead of root in intance viewer

AUTO UPDATER
-------------------------
DRAFT:

# PAPER UPDATE BUTTON (OPEN A TAB TO SELECT AVAILABLE BUILDS)


# DETAILS
Detect instance type (vanilla, paper or velocity) from the given root dir itself while adding a instance.
-remove "NODES ONLINE | INSTANCES | RUNNING | NEEDS ATTENTION" from overview

have ping in overview

# UI ONLY
remove the "STATE" from the instances viewer (when inspacting a node)
replace previous "STATE" with "VERSION" in the instances viewer. it will display the version the server is running
a way to do it: grab that from "<server_root>/cache/mojang_VERSION.jar" ex: "<server_root>/cache/mojang_1.21.6.jar" or "<server_root>/cache/mojang_26.2.jar". if there is more than one mojang jars grab the latest. if you have a better way to verify witch version is running, use yours.


**NEW TAB** instance > BACKUPS
only when server is stopped?
.gz compressed (include worlds dirs and plugin dir)
run a sanity-check in the compressed file after backup to see if it is corrupted
auto-backup
weekly? daily?
max GB allowed? max retention files?


NEW TAB instance > PROPERTIES (vanilla/paper only) (remove?)
display a nicer server properties.
all fields are in a row, with editable fields to fill in. no hardcoded server.properties default to avoid version conflicts.
the only nice thing we can add that Always remains the same is to make ON/OFF switches in the UI for bools in server.properties.


NEW TAB - PERFORMANCE
sdsadsadsas


# HUB

# HUB TABS: PERFORMANCE / STATISTICS (player activity, peak hours)
filters for whole network, individual nodes, and individual instances

USE SPARK

# PERFOMANCE: IN "HUB OVERVIEW" AND "NODE VIEWER" AND "<instance> > <tab> PERFORMANCE"(inherits filtered from global health Checker?)
metrics:
-tps/ping health inspector
-disk-capacity
-RAM CPU inspector
-network outbound / inbound


in the main HUB OVERVIEW, it displays a summary of these metrics by node. so, for node "proxy" with instances "velocity" and "velocity_premium", 
and node "main-server" with instances "dev" and "main", overview would display the sum of all metrics of the instances of that node.
in "NODE VIEWER"

# STATISTICS:

metrics:

-top players?
-peak hours
-record player count


V0.2.0:
create a new server


V0.3.0:
mobile lightweight version. no file editor or advanced configs; only console and basic things, but fluid and seamless

V0.4.0:
security update

ATTACK DETECTION
THREAT ASSESMENT
DDOS/BROKEN PACKET ATTACKS

