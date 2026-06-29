import os
import asyncio
import threading
import psycopg2
from psycopg2.extras import DictCursor
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load variables from the local environment
load_dotenv()

# --- CONFIGURATION (SECURED) ---
TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID"))
LURKER_ROLE_ID = int(os.getenv("LURKER_ROLE_ID"))
CATEGORY_ID = int(os.getenv("CATEGORY_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")  # 🟢 New Cloud Database URL

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class VerificationBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.attempts_tracker = {}  # Tracks failure strikes per channel ID
        
    async def setup_hook(self):
        # Initialize SQL Database Tables Schema
        init_db_schema()
        
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
        
        self.auto_roster_updater.start()
        print("⏰ 3-Minute Background Roster Updater Loop Active")
        
        print("🌐 Starting background keep-alive web server...")
        threading.Thread(target=run_web_server, daemon=True).start()

    @tasks.loop(minutes=3.0)
    async def auto_roster_updater(self):
        await self.wait_until_ready()
        try:
            await update_verification_log_board()
        except Exception as e:
            print(f"❌ Background roster auto-sync failed: {e}")

bot = VerificationBot()

# --- SQL DATABASE COMPONENT ENGINE ---
def get_db_connection():
    """Establishes an active reference connection pool handle directly to cloud SQL"""
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db_schema():
    """Initializes and builds the target database table matrix schema layer if missing"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            name TEXT NOT NULL,
            student_number TEXT PRIMARY KEY,
            discord_id VARCHAR(32),
            alt_id_1 VARCHAR(32),
            alt_id_2 VARCHAR(32),
            password TEXT
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("🐘 Cloud Postgres Database Synced and Verified.")

def clean_student_number(student_num: str) -> str:
    parts = student_num.strip().split('-')
    if len(parts) >= 2:
        return parts[1] 
    return student_num.strip()[-5:] 

def get_all_verified_entries() -> list[str]:
    entries = []
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    # Query all students who have any account linked
    cur.execute("""
        SELECT name, student_number FROM students 
        WHERE (discord_id IS NOT NULL AND discord_id != '')
           OR (alt_id_1 IS NOT NULL AND alt_id_1 != '')
           OR (alt_id_2 IS NOT NULL AND alt_id_2 != '')
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    for row in rows:
        raw_num = row['student_number'].strip()
        raw_name = row['name'].strip().upper()
        
        five_digit = clean_student_number(raw_num)
        if len(five_digit) >= 2:
            censored_digits = five_digit[:-2] + "XX"
        else:
            censored_digits = "XX"
        entries.append((raw_name, f"🟢 `[ {raw_name} ]` `[ {censored_digits} ]` ACTIVE"))
        
    entries.sort(key=lambda x: x[0])
    return [line[1] for line in entries]

def check_and_verify_student(student_num: str, discord_id: str) -> tuple[str, str, str]:
    user_five_digit = clean_student_number(student_num)
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    # Check for match via full number or short sub-chunk digits
    cur.execute("SELECT * FROM students WHERE student_number = %s OR student_number LIKE %s", 
                (student_num.strip(), f"%{user_five_digit}%"))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return "not_found", user_five_digit, "STUDENT"
        
    db_num = row['student_number']
    final_five_digit = clean_student_number(db_num)
    final_name = row['name'].strip().upper()
    
    if row['discord_id'] and row['discord_id'].strip():
        cur.close()
        conn.close()
        return "already_verified", final_five_digit, final_name
        
    # Bind user info cleanly
    default_pass = f"cpe-{final_five_digit}"
    cur.execute("UPDATE students SET discord_id = %s, password = %s WHERE student_number = %s",
                (str(discord_id), default_pass, db_num))
    conn.commit()
    cur.close()
    conn.close()
    return "success", final_five_digit, final_name

def process_dummy_login(student_num: str, provided_pass: str, discord_id: str) -> tuple[str, str]:
    user_five_digit = clean_student_number(student_num)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    cur.execute("SELECT * FROM students WHERE student_number = %s OR student_number LIKE %s", 
                (student_num.strip(), f"%{user_five_digit}%"))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return "not_found", "Student number not found."
        
    db_num = row['student_number']
    final_five_digit = clean_student_number(db_num)
    stored_pass = row['password'].strip() if row['password'] else f"cpe-{final_five_digit}"
    
    if stored_pass != provided_pass.strip():
        cur.close()
        conn.close()
        return "wrong_password", "Invalid password credential provided."
        
    str_id = str(discord_id)
    if row['discord_id'] == str_id or row['alt_id_1'] == str_id or row['alt_id_2'] == str_id:
        cur.close()
        conn.close()
        return "already_linked", "Account already registered! This specific Discord account is already active somewhere on this student profile."
        
    if not row['alt_id_1'] or not row['alt_id_1'].strip():
        cur.execute("UPDATE students SET alt_id_1 = %s WHERE student_number = %s", (str_id, db_num))
        status, msg = "success", "You've successfully entered the Valhalla!"
    elif not row['alt_id_2'] or not row['alt_id_2'].strip():
        cur.execute("UPDATE students SET alt_id_2 = %s WHERE student_number = %s", (str_id, db_num))
        status, msg = "success", "You've successfully entered the Valhalla!"
    else:
        status, msg = "max_slots", "If full, you've reached the threshold of alt accounts, young one."
        
    conn.commit()
    cur.close()
    conn.close()
    return status, msg

def process_self_reset(student_num: str, discord_id: str) -> tuple[bool, str]:
    user_five_digit = clean_student_number(student_num)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    cur.execute("SELECT * FROM students WHERE student_number = %s OR student_number LIKE %s", 
                (student_num.strip(), f"%{user_five_digit}%"))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return False, "Student record not found or your Discord ID doesn't match the primary slot."
        
    db_num = row['student_number']
    final_five_digit = clean_student_number(db_num)
    
    if row['discord_id'] != str(discord_id):
        cur.close()
        conn.close()
        return False, "Security block: Only the primary verified Discord holder can trigger an auto-reset modal."
        
    cur.execute("UPDATE students SET password = %s WHERE student_number = %s", (f"cpe-{final_five_digit}", db_num))
    conn.commit()
    cur.close()
    conn.close()
    return True, f"cpe-{final_five_digit}"

def process_interactive_change(discord_id: str, old_pass: str, new_pass: str) -> tuple[bool, str]:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    cur.execute("SELECT * FROM students WHERE discord_id = %s", (str(discord_id),))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return False, "Your account is not verified as the primary holder of any profile."
        
    stored_pass = row['password'].strip() if row['password'] else f"cpe-{clean_student_number(row['student_number'])}"
    if stored_pass != old_pass.strip():
        cur.close()
        conn.close()
        return False, "Invalid current password credential provided."
        
    cur.execute("UPDATE students SET password = %s WHERE discord_id = %s", (new_pass.strip(), str(discord_id)))
    conn.commit()
    cur.close()
    conn.close()
    return True, "Success"

async def prune_old_user_tickets(guild: discord.Guild, member: discord.Member):
    category = guild.get_channel(CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel): return
    
    target_prefixes = (f"verify-{member.name.lower()}", f"forgot-pass-{member.name.lower()}")
    for channel in category.text_channels:
        if channel.name.lower().startswith(target_prefixes):
            try: await channel.delete(reason="Pruned old ticket to enforce single concurrent channel policy.")
            except: pass

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
                "**Primary Account Verification:**\n"
                "➡️ Click **📝 Verify Roster Profile** to register your main account.\n\n"
                "**Alternate / Dummy Account Links:**\n"
                "➡️ Click **🔓 Link Alternate Account** to connect a secondary profile.\n\n"
                "⚠️ *This ticket channel automatically self-destructs after **3 minutes** or after **3 failed verification strikes**.*"
            ), 
            color=discord.Color.from_rgb(231, 76, 60) 
        ) 
        
        # 🟢 Send the primary verification buttons first
        await ticket_channel.send(embed=embed, content=member.mention, view=TicketActionView()) 
        
        # 🟢 IMMEDIATELY SEND THE ACCOUNT MANAGEMENT TOOLS INSIDE THE SAME TICKET ROOM!
        management_embed = discord.Embed(
            title="🔐 Profile Credential Management Menu",
            description="If you are configuring or resetting an existing profile structure, utilize the options below:",
            color=discord.Color.orange()
        )
        await ticket_channel.send(embed=management_embed, view=ForgotPassActionView())

        await interaction.followup.send(f"✅ Onboarding entry gate open: {ticket_channel.mention}", ephemeral=True) 
        bot.loop.create_task(channel_lifespan_timer(ticket_channel.id)) 

    # 🔄 You can safely leave this here or remove it if you don't want a separate button on the main panel anymore!
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
    success, response_msg = process_interactive_change(interaction.user.id, old_password, new_password)
    if success:
        await interaction.followup.send("✅ Password updated successfully! Dummy accounts can now use this credential.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Update rejected: {response_msg}", ephemeral=True)

@bot.tree.command(name="reset_password", description="Admin exclusive tool to manually override profile credentials.")
@app_commands.describe(student_number="Target student number", new_password="New password string")
@app_commands.checks.has_permissions(administrator=True)
async def reset_password(interaction: discord.Interaction, student_number: str, new_password: str):
    await interaction.response.defer(ephemeral=True)
    user_five_digit = clean_student_number(student_number)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    cur.execute("SELECT student_number FROM students WHERE student_number = %s OR student_number LIKE %s", 
                (student_number.strip(), f"%{user_five_digit}%"))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        await interaction.followup.send("❌ Student number not found.", ephemeral=True)
        return
        
    cur.execute("UPDATE students SET password = %s WHERE student_number = %s", (new_password.strip(), row['student_number']))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.followup.send(f"✅ Password for `{row['student_number']}` has been manually overridden.", ephemeral=True)

# --- ADMIN SETUP UTILITIES --- 
@bot.command() 
@commands.has_permissions(administrator=True) 
async def setup_panel(ctx): 
    permissions = ctx.channel.permissions_for(ctx.guild.me)
    if not permissions.send_messages or not permissions.embed_links:
        try: await ctx.author.send(f"❌ Error: Missing standard app permissions.")
        except discord.Forbidden: pass
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
        try: await ctx.message.delete() 
        except discord.DiscordException: pass

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