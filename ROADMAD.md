ROADMAD v0.1.0 - v0.1.5
fixes and improvements
---------------------------------------

# A) FILE BROWSER/EDITOR

1-Add a button to expand file editor to make it fullscreen (take up the whole screen) and go back to normal after
3-Add a Upload files in the file tree viewer. Let's make to ways to add a file:
 a) drag and drop in the file tree viewer to add a file to that dir.
 b) a small Upload button next to "+" (create new file) button, so the user can chose a file if they dont want to drop it.

2-Add a button to Download a file (with its respecting endpoint if it doesn't already exist) next to the new Upload button.

3-Add a safe-guard to large files and instead of immediatly loading them and displaying in the file editor:
 a)If the user selects a large file, prompt and warn the user about the action to see if they want to proceed with it. That avoids unwanted freezes if they click a large file by mistake
 b) Do not load or display .jar files in the file editor.

4- Make a dedicated endpoint for large file transfers so worlds can be uploaded
5- Make a way to extract files, so worlds that are tipically large files, up to GBs, can be uploaded as well

6- Add right click options in File Explorer Delete, download, rename, extract (if compressed (.rar, zip, .gz)) thats just a quick shortcut to the main static buttons that do those actions in the current selected file; the only difference being it does those actions to the right clicked file. 

# B) VERSION & BUILD UPDATER

## Create a new Version tab inside the Instance page.

The purpose of this feature is to upgrade or downgrade the server software while preserving the existing server files.

This feature does not change the server type. A Paper server remains Paper, a Vanilla server remains Vanilla, and a Velocity server remains Velocity.

## Supported software:

1. Vanilla
2. Paper
3. Velocity

## Available Versions

Fetch the list of available releases appropriate for the current server type.

### Vanilla

Minecraft Version

Example:

Version
▼ 1.21.6

### Paper

Minecraft Version
Available Paper Builds for the selected Minecraft version

Example:

Minecraft Version
▼ 1.21.6

Paper Build
▼ Build 62

Changing the Minecraft version must automatically refresh the list of available Paper builds.

### Velocity

Display the available Velocity releases.

If Velocity exposes build information separately, display both Version and Build. Otherwise, only display Version.

The UI should adapt to the available metadata rather than assuming every software exposes builds.

### Update Workflow

When the user presses Update:

1. Verify the instance is stopped.
2. Refuse the operation if the server is currently running. (and disable the start command if an update is undergoing -- add a UI indicator for that case)
3. Download the selected server binary. (determine an timeout so we cannot get stuck in an infinite update)
4. Create a backup of the currently configured server JAR.
5. Replace the configured server JAR.
6. Refresh the Version Detector metadata. (SKIP FOR NOW WITH A PLACEHOLDER, WE WILL BUILD Version Detector LATER)
7. Update the displayed version.
8. Report success or any encountered errors.

## The updater must never modify:

worlds
plugins
mods
configuration files
player data
logs
any user-generated content

Only the server executable should be replaced.

## Rollback Safety

Before replacing the server JAR, create a backup of the previous executable.

If the replacement fails for any reason, automatically restore the previous JAR.

The operation should be transactional whenever possible.

## UI

You are capable of amazing desing and UI work, based on our project's style, create a quality Update tab.

Display:

Current Version
Current Build (when applicable)
Target Version selector
Target Build selector (only when applicable)
Update button

Example (Paper):

Current
Minecraft 1.21.6
Paper Build 62

Target

Minecraft Version
▼ 1.21.7

Paper Build
▼ Build 15

[ Update ]

Example (Vanilla):

Current
Minecraft 1.21.6

Target

Version
▼ 1.21.7

[ Update ]

The interface should automatically hide controls that are not applicable to the current server type.

## Architecture

The updater must be software-agnostic.

Implement a provider-based architecture where each supported server type supplies:

Available versions
Available builds (if applicable)
Download URL
Metadata required for installation

The UI should consume a common interface rather than containing Paper-, Vanilla-, or Velocity-specific logic.

Adding support for additional server software in the future (for example Purpur, Folia, Fabric, Forge, NeoForge, etc.) should only require implementing a new provider without modifying the updater UI.

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

bonus: feat: render png in file explorer

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

2-Add a online-mode ON/OFF switch (ON= premium only, OFF= allow cracked) in the instance Settings that controls "online-mode" bool in server.propeties. It must always reflect the state of the file, there shouldn't be a way for the Settings and the actual file to be desynced.

3-Add world type in settings so user can change worldtype (flat, normal,). If changed, show a little warning that they might have to delete their current world for the config to make effect.

4-Add RAM slider (we have it while Adding a new instance) into settings, using our already written logic to keep the -Xmx arg in sync with the slider

5-remove the hover effect on the slider
-----------------------------------------
BUGS:

when you change from instance A to instance B, while viewing instance's A file exporer, the file being shown doesn't change. Ideally we shouldclose the current file being presented when changing instances.

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

