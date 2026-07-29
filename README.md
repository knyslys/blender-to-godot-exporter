Blender exporter for Godot

Hey guys. I vibesloped this simple Blender python addon to export asset packs and 3d models into Godot engine kinda easily without wanting to hang myself. 

It exports each object or collection as individual `.glb` file so u can drag them straight into Godot.

It also **automatically** adds collision suffixes, fixes pivot points to bottom center, checks for broken textures, and it can run your import scripts when exporting.

It also detects the same textures used by other exported materials, so it wont be duplicated.
---

## How to Install

1. Download `godot_scene_exporter.py` file.
2. Open Blender, go to **Edit > Preferences > Add-ons**.
3. Click **Install...** at top right and select `godot_scene_exporter.py`.
4. Check the box to enable **Godot Scene Exporter**.
5. Create `Scripts` folder at the root of your Godot project.
6. Add `godot_post_import.gd` into that folder. When you exporting from Blender, you can select this script in the addon menu (or you just can generate this script from the addon menu)
   **What it does?** It extracts the materials into `Materials` folder, detects same material used by other objects and wont duplicate them. So if 2 objects or more share 1 material, it wont be duplicated. Also fix static bodies collisions.
7. **(IMPORTANT)** WHEN YOU ADD THAT SCRIPT TO YOUR GODOT FOLDER, MAKE SURE TO OPEN IT ATLEAST ONCE. Also, atleast for me, materials folder doesnt appear until i check that directory through file explorer.

---

## How to Use

1. Open **3D Viewport** in Blender and press `N` key to open the sidebar.
2. Look for **Godot Export** tab.
3. Choose your **Output Folder** (for example `YourProject/Assets/YourAssets`)
4. Select your **Export Mode**:
   - **Selected Objects**: Export selected items + children.
   - **All Collections**: Export each collection into its own `.glb` file.
5. Pick your **Collision Mode**.
6. Pick **Pivot / Origin** (set to Bottom Center if you want floor snapping).
7. *(Optional)* Toggle **Generate .import Sidecars** and select a post import `.gd` script if u have one.
8. Click **Run Preflight Health Check** to make sure no broken textures or unapplied scales!
9. Click **Export Asset Pack for Godot!**

 **(IMPORTANT)** MAKE SURE TO NAME YOUR TEXTURES WITH UNIQUE NAMES. TEXTURES NAMED Texture001, Texture002 will be considered as the same and wont be duplicated!

---
<img width="800" height="481" alt="1" src="https://github.com/user-attachments/assets/3f3047e4-725d-468d-8e7e-11cd144c1478" />
<img width="800" height="357" alt="ezgif com-video-to-gif-converter" src="https://github.com/user-attachments/assets/62972f39-0f9c-4c4f-b110-bca5e6f224ef" />
