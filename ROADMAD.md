ROADMAD v0.1.0 - v0.1.5
fixes and improvements
---------------------------------------

v0.1.1

# A) CONSOLE
1-The console history is not available when a new web session is acessing Console. We can build a endpoint to grab from latest to fill their console with previous data from that session, that way we can show what happened before the client acessed the console. But we do not use it for anything else, when previous data is successfully loaded, we start showing live data as we would normally. If there is an error while getting or displaying data from latest, ignore it -- this should not break our console if it fails.

2-After fix: Replace "This server was already running when the agent attached — console output only streams for sessions the agent started. Restart the instance to attach the console." UI warn with an Error for when (or if) the history ever fails to load. New UI warn "Failed to load console session history." or something along those lines.


# B) ADDING A NEW INSTANCE
1-Build/change the name of the .jar file being used in the start command "java -Xmx4G -jar paper.jar nogui" from the identified type.

So:
Velocity uses velocity.jar,
Paper uses paper.jar
Vanilla uses server.jar

2-Add a little editable memory allocation indicator (based on "-Xmx4G" arg in Start Command) so the user can easily see and change how much RAM they are setting up the server with. The arg "-Xmx4G" and amount of RAM in the indicator cannot desync never, cause they are the same thing essentially.

3-Remove RCON configuration while adding a new Instance, leave it only in settings.

# C) FILE BROWSER/EDITOR

1-Add a button to expand file editor to make it fullscreen
2-Add a button to Download a file (with its respecting endpoint) next to the new Upload button.
3-Add a Upload files in the file tree viewer. Let's make to ways to add a file:
 a) drag and drop in the file tree viewer to add a file to that dir.
 b) a small Upload button next to "+" (create new file) button, so the user can chose a file if they dont want to drop it.

4-Add a safe-guard to large files and instead of immediatly loading them and displaying in the file editor:
If the user selects a large file, prompt and warn the user about the action to see if they want to proceed with it. That avoids unwanted freezes if they click a large file by mistake
6-Do not load or display .jar files in the file editor.
7-the app cannot have access to other dirs? add mandatory non-escapable root for each node?

Add right click options? Delete, download, rename, extract (if compressed (.rar, zip, .gz))
Make a way to extract files too?


# D) SETTINGS UPDATE [VERSION DEPENDENT?]
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

2-Add a online-mode ON/OFFswitch (ON= premium only, OFF= allow cracked) in the instance Settings that controls "online-mode" bool in server.propeties. It must always reflect the state of the file, there shouldn't be a way for the Settings and the actual file to be desynced.



-----------------------------------------
DRAFT:


# DETAILS
Detect instance type (vanilla, paper or velocity) from the given root dir itself while adding a instance.
-remove "NODES ONLINE | INSTANCES | RUNNING | NEEDS ATTENTION" from overview

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

