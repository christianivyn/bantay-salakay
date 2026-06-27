import csv
import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load variables from the hidden local .env file
load_dotenv()

# --- CONFIGURATION (SECURED) ---
TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID"))
LURKER_ROLE_ID = int(os.getenv("LURKER_ROLE_ID"))
CATEGORY_ID = int(os.getenv("CATEGORY_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

CSV_FILE = "students.csv"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class VerificationBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.attempts_tracker = {}  # Tracks failure strikes per channel ID
        
    async def setup_hook(self):
        self.add_view(PersistentPanel())
        self.add_view(AdminTicketView())
        self.add_view(TicketActionView())
        self.add_view(ForgotPassActionView())
        print("🔒 Persistent Views Registered")
        
        print("🔄 Syncing global command tree matrix...")
        try:
            synced = await self.tree.sync()
            print(f"✅ Global Command Matrix Synced: {len(synced)} entries live.")
        except Exception as e:
            print(f"❌ Failed to sync command tree: {e}")
        
        print("🌐 Starting background keep-alive web server...")
        threading.Thread(target=run_web_server, daemon=True).start()

bot = VerificationBot()

# --- BACKEND LOGIC ---
def clean_student_number(student_num: str) -> str:
    parts = student_num.strip().split('-')
    if len(parts) >= 2:
        return parts[1] 
    return student_num.strip()[-5:] 

def get_all_verified_entries() -> list[str]:
    if not os.path.exists(CSV_FILE):
        return []
    entries = []
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned_row = {k.strip(): v for k, v in row.items() if k}
            if (cleaned_row.get('discord_id') and cleaned_row['discord_id'].strip()) or \
               (cleaned_row.get('alt_id_1') and cleaned_row['alt_id_1'].strip()) or \
               (cleaned_row.get('alt_id_2') and cleaned_row['alt_id_2'].strip()):
                
                raw_num = cleaned_row.get('student_number', '').strip()
                raw_name = cleaned_row.get('name', '').strip().upper()
                
                if raw_num and raw_name:
                    five_digit = clean_student_number(raw_num)
                    if len(five_digit) >= 2:
                        censored_digits = five_digit[:-2] + "XX"
                    else:
                        censored_digits = "XX"
                    entries.append((raw_name, f"🟢 `[ {raw_name} ]` `[ {censored_digits} ]` ACTIVE"))
    entries.sort(key=lambda x: x[0])
    return [line[1] for line in entries]

def check_and_verify_student(student_num: str, discord_id: str) -> tuple[str, str, str]:
    if not os.path.exists(CSV_FILE):
        return "not_found", "", ""

    rows = []
    found = False
    status = "not_found"
    final_five_digit = clean_student_number(student_num)
    final_name = "STUDENT"
    
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        raw_fields = list(reader.fieldnames) if reader.fieldnames else []
        field_map = {f.replace('\ufeff', '').strip(): f for f in raw_fields}
        
        for col in ['name', 'student_number', 'discord_id', 'alt_id_1', 'alt_id_2', 'password']:
            if col not in field_map:
                field_map[col] = col
                raw_fields.append(col)

        for row in reader:
            cleaned_row = {k.replace('\ufeff', '').strip(): v for k, v in row.items() if k}
            
            csv_num = cleaned_row.get('student_number', '').strip()
            if csv_num == student_num.strip():
                found = True
                final_five_digit = clean_student_number(csv_num)
                final_name = cleaned_row.get('name', '').strip().upper()
                
                if cleaned_row.get('discord_id') and cleaned_row['discord_id'].strip():
                    status = "already_verified"
                else:
                    cleaned_row['discord_id'] = str(discord_id)
                    cleaned_row['password'] = f"cpe-{final_five_digit}"
                    status = "success"
            
            out_row = {original_header: cleaned_row.get(target_clean, '') for target_clean, original_header in field_map.items()}
            rows.append(out_row)
            
    if found and status == "success":
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=raw_fields)
            writer.writeheader()
            writer.writerows(rows)
            
    return status, final_five_digit, final_name

def process_dummy_login(student_num: str, provided_pass: str, discord_id: str) -> tuple[str, str]:
    if not os.path.exists(CSV_FILE): return "not_found", "Database missing."

    rows = []
    status = "not_found"
    msg = "Student number not found."
    final_five_digit = clean_student_number(student_num)
    
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        raw_fields = list(reader.fieldnames) if reader.fieldnames else []
        field_map = {f.replace('\ufeff', '').strip(): f for f in raw_fields}
        
        for col in ['name', 'student_number', 'discord_id', 'alt_id_1', 'alt_id_2', 'password']:
            if col not in field_map:
                field_map[col] = col
                raw_fields.append(col)
        
        for row in reader:
            cleaned_row = {k.replace('\ufeff', '').strip(): v for k, v in row.items() if k}

            csv_num = cleaned_row.get('student_number', '').strip()
            if csv_num == student_num.strip():
                stored_pass = cleaned_row.get('password', '').strip()
                if not stored_pass:
                    stored_pass = f"cpe-{final_five_digit}"
                
                if stored_pass != provided_pass.strip():
                    status = "wrong_password"
                    msg = "Invalid password credential provided."
                    out_row = {field_map[k]: v for k, v in cleaned_row.items()}
                    rows.append(out_row)
                    continue
                
                str_id = str(discord_id)
                
                # 🔒 FIXED MULTI-SLOT BLOCKER: Rejects if THIS exact account already occupies ANY slot under this profile
                if cleaned_row.get('discord_id') == str_id or \
                   cleaned_row.get('alt_id_1') == str_id or \
                   cleaned_row.get('alt_id_2') == str_id:
                    status = "already_linked"
                    msg = "Account already registered! This specific Discord account is already active somewhere on this student profile."
                    out_row = {field_map[k]: v for k, v in cleaned_row.items()}
                    rows.append(out_row)
                    continue

                # Slot checking matrix logic
                if not cleaned_row.get('alt_id_1') or not cleaned_row['alt_id_1'].strip():
                    cleaned_row['alt_id_1'] = str_id
                    status = "success"
                    msg = "You've successfully entered the Valhalla!"
                elif not cleaned_row.get('alt_id_2') or not cleaned_row['alt_id_2'].strip():
                    cleaned_row['alt_id_2'] = str_id
                    status = "success"
                    msg = "You've successfully entered the Valhalla!"
                else:
                    status = "max_slots"
                    msg = "If full, you've reached the threshold of alt accounts, young one."
            
            out_row = {original_header: cleaned_row.get(target_clean, '') for target_clean, original_header in field_map.items()}
            rows.append(out_row)

    if status == "success":
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=raw_fields)
            writer.writeheader()
            writer.writerows(rows)

    return status, msg

def process_self_reset(student_num: str, discord_id: str) -> tuple[bool, str]:
    if not os.path.exists(CSV_FILE): return False, "Database reference matrix missing."
    rows = []
    success = False
    error_msg = "Student record not found or your Discord ID doesn't match the primary slot."
    final_five_digit = clean_student_number(student_num)

    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        raw_fields = list(reader.fieldnames) if reader.fieldnames else []
        field_map = {f.replace('\ufeff', '').strip(): f for f in raw_fields}

        for row in reader:
            cleaned_row = {k.replace('\ufeff', '').strip(): v for k, v in row.items() if k}
            if cleaned_row.get('student_number', '').strip() == student_num.strip():
                if cleaned_row.get('discord_id', '').strip() == str(discord_id):
                    cleaned_row['password'] = f"cpe-{final_five_digit}"
                    success = True
                else:
                    error_msg = "Security block: Only the primary verified Discord holder can trigger an auto-reset modal."
            
            out_row = {original_header: cleaned_row.get(target_clean, '') for target_clean, original_header in field_map.items()}
            rows.append(out_row)

    if success:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=raw_fields)
            writer.writeheader()
            writer.writerows(rows)
        return True, f"cpe-{final_five_digit}"
    return False, error_msg

def process_interactive_change(discord_id: str, old_pass: str, new_pass: str) -> tuple[bool, str]:
    if not os.path.exists(CSV_FILE): return False, "Database roster file missing."
    rows = []
    updated = False
    error_msg = "Your account is not verified as the primary holder of any profile."

    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        raw_fields = list(reader.fieldnames) if reader.fieldnames else []
        field_map = {f.replace('\ufeff', '').strip(): f for f in raw_fields}

        for row in reader:
            cleaned_row = {k.replace('\ufeff', '').strip(): v for k, v in row.items() if k}
            if cleaned_row.get('discord_id', '').strip() == str(discord_id):
                stored_pass = cleaned_row.get('password', '').strip()
                if not stored_pass:
                    stored_pass = f"cpe-{clean_student_number(cleaned_row.get('student_number', ''))}"
                
                if stored_pass == old_pass.strip():
                    cleaned_row['password'] = new_pass.strip()
                    updated = True
                else:
                    error_msg = "Invalid current password credential provided."
            
            out_row = {original_header: cleaned_row.get(target_clean, '') for target_clean, original_header in field_map.items()}
            rows.append(out_row)

    if updated:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=raw_fields)
            writer.writeheader()
            writer.writerows(rows)
        return True, "Success"
    return False, error_msg

async def prune_old_user_tickets(guild: discord.Guild, member: discord.Member):
    category = guild.get_channel(CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel): return
    
    target_prefixes = (f"verify-{member.name.lower()}", f"forgot-pass-{member.name.lower()}")
    for channel in category.text_channels:
        if channel.name.lower().startswith(target_prefixes):
            try:
                await channel.delete(reason="Pruned old overlapping ticket to enforce single concurrent channel policy.")
            except:
                pass

async def handle_strike_count(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    if channel_id not in bot.attempts_tracker:
        bot.attempts_tracker[channel_id] = 0
        
    bot.attempts_tracker[channel_id] += 1
    remaining_strikes = 3 - bot.attempts_tracker[channel_id]
    
    if remaining_strikes <= 0:
        if channel_id in bot.attempts_tracker:
            del bot.attempts_tracker[channel_id]
        await interaction.followup.send("❌ **Security Lockdown:** Out of attempts! This ticket will self-destruct in 5 seconds...", ephemeral=True)
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Excessive verification tracking errors.")
    else:
        await interaction.followup.send(f"❌ Verification mismatch data. You have **{remaining_strikes}** attempts remaining.", ephemeral=True)

async def update_verification_log_board():
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return

    verified_lines = get_all_verified_entries()
    description_text = "\n".join(verified_lines) if verified_lines else "*No verified classmates registered yet.*"

    embed = discord.Embed(
        title="📋 CPE 1-1 Official Verification Roster Status",
        description=description_text,
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Total Profiles Engaged: {len(verified_lines)}")

    message_found = False
    try:
        async for msg in log_channel.history(limit=50):
            if msg.author == bot.user and msg.embeds and "Official Verification Roster Status" in msg.embeds[0].title:
                await msg.edit(embed=embed)
                message_found = True
                break
        if not message_found: await log_channel.send(embed=embed)
    except Exception as e: print(f"❌ Log sync failure: {e}")

async def channel_lifespan_timer(channel_id: int):
    await asyncio.sleep(180)
    try:
        channel = bot.get_channel(channel_id)
        if not channel: channel = await bot.fetch_channel(channel_id)
        if channel: 
            await channel.send("⏰ **Time limit reached!** Closing this onboarding terminal channel...")
            await asyncio.sleep(3)
            await channel.delete(reason="Ticket lifecycle time limit expired.")
    except: pass 

# --- POPUP DIALOG MODALS ---
class VerifyModal(discord.ui.Modal, title="🎫 Master Account Verification"):
    student_number = discord.ui.TextInput(label="Official Student Number", placeholder="e.g., 2025-00000-BN-0", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result, five_digit, full_name = check_and_verify_student(self.student_number.value, interaction.user.id)
        
        if result == "not_found":
            await handle_strike_count(interaction)
        elif result == "already_verified":
            await interaction.followup.send(
                "⚠️ **Account Already Registered!**\n"
                "This student number is already verified to a primary Discord account. "
                "Perhaps you are trying to connect using an alt or dummy account? If so, please close this popup and use the **Link Alternate Account** button instead!", 
                ephemeral=True
            )
        elif result == "success":
            role = interaction.guild.get_role(ROLE_ID)
            lurker = interaction.guild.get_role(LURKER_ROLE_ID)
            if role:
                await interaction.user.add_roles(role)
                if lurker and lurker in interaction.user.roles: await interaction.user.remove_roles(lurker)
                await interaction.followup.send(f"🎉 Welcome aboard **{full_name}**! Default password set to: `cpe-{five_digit}`. Closing channel...", ephemeral=True)
                await update_verification_log_board()
                await asyncio.sleep(5)
                await interaction.channel.delete(reason="Primary verification completed.")

class LoginModal(discord.ui.Modal, title="🔓 Login Alternate Account"):
    student_number = discord.ui.TextInput(label="Official Student Number", placeholder="e.g., 2025-00000-BN-0", required=True)
    password = discord.ui.TextInput(label="Your Profile Password", placeholder="Enter your secure password", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        res, message = process_dummy_login(self.student_number.value, self.password.value, interaction.user.id)
        
        if res in ["not_found", "wrong_password"]:
            await handle_strike_count(interaction)
        elif res in ["max_slots", "already_linked"]:
            # Returns custom full limit bounds or duplication errors seamlessly
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        elif res == "success":
            role = interaction.guild.get_role(ROLE_ID)
            lurker = interaction.guild.get_role(LURKER_ROLE_ID)
            if role:
                await interaction.user.add_roles(role)
                if lurker and lurker in interaction.user.roles: await interaction.user.remove_roles(lurker)
                await interaction.followup.send(f"🎉 **Access Granted:** {message} Closing channel...", ephemeral=True)
                await update_verification_log_board()
                await asyncio.sleep(5)
                await interaction.channel.delete(reason="Dummy registration completed.")

class SelfResetModal(discord.ui.Modal, title="🔄 Reset Profile Password"):
    student_number = discord.ui.TextInput(label="Confirm Student Number", placeholder="e.g., 2025-00000-BN-0", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        success, payload = process_self_reset(self.student_number.value, interaction.user.id)
        if success:
            await interaction.followup.send(f"✅ **Success!** Your profile password has been restored to default: `{payload}`. Closing ticket room...", ephemeral=True)
            await asyncio.sleep(5)
            await interaction.channel.delete(reason="Password restored autonomously by user.")
        else:
            await handle_strike_count(interaction)

class CustomChangeModal(discord.ui.Modal, title="🔑 Change Custom Password"):
    old_password = discord.ui.TextInput(label="Current Password", placeholder="Enter your current password", required=True)
    new_password = discord.ui.TextInput(label="New Secure Password", placeholder="Enter your new secret password", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        success, response_msg = process_interactive_change(interaction.user.id, self.old_password.value, self.new_password.value)
        if success:
            await interaction.followup.send("✅ **Success!** Your account password has been updated. Closing ticket room...", ephemeral=True)
            await asyncio.sleep(5)
            await interaction.channel.delete(reason="Password changed autonomously via ticket dashboard system.")
        else:
            await handle_strike_count(interaction)

# --- TICKET BUTTON INTERFACES ---
class TicketActionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="📝 Verify Roster Profile", style=discord.ButtonStyle.success, custom_id="ticket_verify_btn")
    async def ticket_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())

    @discord.ui.button(label="🔓 Link Alternate Account", style=discord.ButtonStyle.primary, custom_id="ticket_login_btn")
    async def ticket_login(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LoginModal())

class ForgotPassActionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Reset to Default Password", style=discord.ButtonStyle.success, custom_id="ticket_self_reset_btn")
    async def ticket_self_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SelfResetModal())

    @discord.ui.button(label="🔑 Change Custom Password", style=discord.ButtonStyle.primary, custom_id="ticket_custom_change_btn")
    async def ticket_custom_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomChangeModal())

    @discord.ui.button(label="🚨 Contact Admin Support", style=discord.ButtonStyle.danger, custom_id="ticket_call_admin_btn")
    async def ticket_call_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.channel.send(content="⚠️ **Manual Support Flagged:** Paging Administration team. Use the lock button below once resolved.", view=AdminTicketView())

class PersistentPanel(discord.ui.View): 
    def __init__(self): super().__init__(timeout=None) 

    @discord.ui.button(label="🎫 Open Verification Gate", style=discord.ButtonStyle.danger, custom_id="start_verification_btn") 
    async def start_verification(self, interaction: discord.Interaction, button: discord.ui.Button): 
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild 
        member = interaction.user 
        category = guild.get_channel(CATEGORY_ID) 
         
        if not category or not isinstance(category, discord.CategoryChannel): 
            await interaction.followup.send("❌ Setup configuration mapping mismatch.", ephemeral=True) 
            return 

        await prune_old_user_tickets(guild, member)

        overwrites = { 
            guild.default_role: discord.PermissionOverwrite(view_channel=False), 
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), 
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True) 
        } 

        ticket_channel = await guild.create_text_channel(name=f"verify-{member.name}", category=category, overwrites=overwrites) 

        embed = discord.Embed( 
            title="🏫 CPE 1-1 Onboarding Gate", 
            description=( 
                f"Welcome {member.mention}!\n\n" 
                "Click **Verify Roster Profile** if this is your primary account.\n"
                "Click **Link Alternate Account** if you are linking a secondary dummy account.\n\n"
                "⚠️ *This ticket channel automatically self-destructs after **3 minutes** or after **3 failed verification strikes**.*"
            ), 
            color=discord.Color.from_rgb(231, 76, 60) 
        ) 
        await ticket_channel.send(embed=embed, content=member.mention, view=TicketActionView()) 
        await interaction.followup.send(f"✅ Onboarding entry gate open: {ticket_channel.mention}", ephemeral=True) 
        bot.loop.create_task(channel_lifespan_timer(ticket_channel.id)) 

    @discord.ui.button(label="⚙️ Manage Account / Password", style=discord.ButtonStyle.secondary, custom_id="forgot_password_btn")
    async def forgot_password(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        category = guild.get_channel(CATEGORY_ID)
        
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("❌ Setup configuration mapping mismatch.", ephemeral=True)
            return

        await prune_old_user_tickets(guild, member)

        overwrites = { 
            guild.default_role: discord.PermissionOverwrite(view_channel=False), 
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), 
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True) 
        }
        
        reset_channel = await guild.create_text_channel(name=f"forgot-pass-{member.name}", category=category, overwrites=overwrites)
        
        embed = discord.Embed(
            title="🔐 Profile Credential Management Menu",
            description=(
                f"Welcome {member.mention} to the security control hub panel.\n\n"
                "**Lost your password?**\n"
                "➡️ Click **Reset to Default Password** to restore your credential back to standard `cpe-[your_5_digits]` format.\n\n"
                "**Want to change it to something private?**\n"
                "➡️ Click **Change Custom Password** to manually declare a brand new secure password string.\n\n"
                "⚠️ *This channel automatically self-destructs after **3 minutes** or after **3 failed verification strikes**.*"
            ),
            color=discord.Color.orange()
        )
        await reset_channel.send(embed=embed, content=member.mention, view=ForgotPassActionView())
        await interaction.followup.send(f"🛠️ Management terminal deployed: {reset_channel.mention}", ephemeral=True)
        bot.loop.create_task(channel_lifespan_timer(reset_channel.id))

class AdminTicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
        
    @discord.ui.button(label="🔒 Close Request Ticket", style=discord.ButtonStyle.grey, custom_id="close_admin_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin authorization required.", ephemeral=True)
            return
        await interaction.response.send_message("🚧 Dissolving support channel context...", ephemeral=True)
        await asyncio.sleep(3)
        await interaction.channel.delete(reason="Manual admin case resolution.")

# --- COMPREHENSIVE COMMAND ACTIONS --- 
@bot.tree.command(name="verify", description="Perform primary user registration via roster index matching.") 
@app_commands.describe(student_number="Enter your complete student number") 
async def verify(interaction: discord.Interaction, student_number: str): 
    if not interaction.channel.name.startswith("verify-"): 
        await interaction.response.send_message("❌ This command must be executed in a verification ticket.", ephemeral=True) 
        return 

    await interaction.response.defer(ephemeral=True) 
    result, five_digit, full_name = check_and_verify_student(student_number, interaction.user.id) 
    
    if result == "not_found": 
        await handle_strike_count(interaction)
    elif result == "already_verified": 
        await interaction.followup.send("⚠️ Account already registered! Perhaps you're using an alt account? If so, use Link Alternate Account instead.", ephemeral=True) 
    elif result == "success": 
        role = interaction.guild.get_role(ROLE_ID) 
        lurker = interaction.guild.get_role(LURKER_ROLE_ID) 
        if role: 
            await interaction.user.add_roles(role) 
            if lurker and lurker in interaction.user.roles: await interaction.user.remove_roles(lurker) 
            await interaction.followup.send(f"🎉 Welcome aboard **{full_name}**! Default password set to: `cpe-{five_digit}`.", ephemeral=True) 
            await update_verification_log_board() 
            await asyncio.sleep(5) 
            await interaction.channel.delete(reason="Verification completed successfully.") 

@bot.tree.command(name="login", description="Authenticate a secondary dummy account using profile credentials.")
@app_commands.describe(student_number="Your master student number", password="Your secure profile password")
async def login(interaction: discord.Interaction, student_number: str, password: str):
    if not interaction.channel.name.startswith("verify-"):
        await interaction.response.send_message("❌ This command must be executed in a verification ticket.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    res, message = process_dummy_login(student_number, password, interaction.user.id)
    
    if res in ["not_found", "wrong_password"]:
        await handle_strike_count(interaction)
    elif res in ["max_slots", "already_linked"]:
        await interaction.followup.send(f"❌ Authentication Rejected: {message}", ephemeral=True)
    elif res == "success":
        role = interaction.guild.get_role(ROLE_ID)
        lurker = interaction.guild.get_role(LURKER_ROLE_ID)
        if role:
            await interaction.user.add_roles(role)
            if lurker and lurker in interaction.user.roles: await interaction.user.remove_roles(lurker)
            await interaction.followup.send(f"🔓 Alt account linked successfully under identity **{message}**!", ephemeral=True)
            await update_verification_log_board()
            await asyncio.sleep(5)
            await interaction.channel.delete(reason="Dummy registration completed.")

@bot.tree.command(name="change_password", description="Update your password using your primary verified Discord account.")
@app_commands.describe(old_password="Your current password", new_password="Your new secure password")
async def change_password(interaction: discord.Interaction, old_password: str, new_password: str):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    
    if not os.path.exists(CSV_FILE):
        await interaction.followup.send("❌ Error: Roster database not found.", ephemeral=True)
        return

    rows = []
    updated = False
    error_msg = "Your current Discord account is not verified as the primary holder of any profile."

    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        
        for row in reader:
            str_id = str(member.id)
            if row.get('discord_id') == str_id:
                stored_pass = row.get('password', '').strip()
                if not stored_pass:
                    stored_pass = f"cpe-{clean_student_number(row.get('student_number', ''))}"
                
                if stored_pass == old_password.strip():
                    row['password'] = new_password.strip()
                    updated = True
                else:
                    error_msg = "Invalid current password provided."
            rows.append(row)

    if updated:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        await interaction.followup.send("✅ Password updated successfully! Dummy accounts can now use this credential.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Update rejected: {error_msg}", ephemeral=True)

@bot.tree.command(name="reset_password", description="Admin exclusive tool to manually override profile credentials.")
@app_commands.describe(student_number="Target student number", new_password="New password string")
@app_commands.checks.has_permissions(administrator=True)
async def reset_password(interaction: discord.Interaction, student_number: str, new_password: str):
    await interaction.response.defer(ephemeral=True)
    if not os.path.exists(CSV_FILE):
        await interaction.followup.send("❌ File structural layer absent.", ephemeral=True)
        return
        
    rows = []
    found = False
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        for row in reader:
            if row.get('student_number', '').strip() == student_number.strip():
                row['password'] = new_password.strip()
                found = True
            rows.append(row)
            
    if found:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        await interaction.followup.send(f"✅ Password for `{student_number}` has been manually changed to `{new_password}`.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Student number not found.", ephemeral=True)

# --- ADMIN SETUP UTILITIES --- 
@bot.command() 
@commands.has_permissions(administrator=True) 
async def setup_panel(ctx): 
    """Admin tool to deploy the main ticket interface panel cleanly"""
    permissions = ctx.channel.permissions_for(ctx.guild.me)
    if not permissions.send_messages or not permissions.embed_links:
        try:
            await ctx.author.send(f"❌ Error: I am missing **Send Messages** or **Embed Links** permissions in {ctx.channel.mention}.")
        except discord.Forbidden:
            pass
        return

    embed = discord.Embed( 
        title="📚 CPE 1-1 Section Gateway", 
        description="Welcome to the digital operations perimeter workspace. Accessing community tools requires verification.", 
        color=discord.Color.from_rgb(231, 76, 60)  
    ) 
    if bot.user.display_avatar:
        embed.set_footer(text="Powered by Bantay Salakay", icon_url=bot.user.display_avatar.url)

    await ctx.send(embed=embed, view=PersistentPanel()) 

    if permissions.manage_messages:
        try: 
            await ctx.message.delete() 
        except discord.DiscordException: 
            pass

@bot.event 
async def on_ready(): 
    print(f"🤖 Operational Status: Connected as {bot.user}") 
    try: 
        await update_verification_log_board() 
        print("📊 Verification status board synchronized.")
    except Exception as e: 
        print(f"❌ Error during roster board sync: {e}") 

# --- KEEP-ALIVE WEB SERVER ROUTINE --- 
class KeepAliveHandler(BaseHTTPRequestHandler): 
    def do_GET(self): 
        self.send_response(200) 
        self.send_header('Content-type', 'text/plain') 
        self.end_headers() 
        self.wfile.write(b"Bantay Salakay is online!") 
    def do_HEAD(self): 
        self.send_response(200) 
        self.send_header('Content-type', 'text/plain') 
        self.end_headers() 
    def log_message(self, format, *args): return 

def run_web_server(): 
    server = HTTPServer(('0.0.0.0', 10000), KeepAliveHandler) 
    server.serve_forever() 

bot.run(TOKEN)