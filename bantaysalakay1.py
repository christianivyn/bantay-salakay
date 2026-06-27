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
        self.attempts_tracker = {}
        
    async def setup_hook(self):
        self.add_view(PersistentPanel())
        self.add_view(AdminTicketView())
        print("🔒 Persistent Views Registered")
        
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
            if (row.get('discord_id') and row['discord_id'].strip()) or \
               (row.get('alt_id_1') and row['alt_id_1'].strip()) or \
               (row.get('alt_id_2') and row['alt_id_2'].strip()):
                
                raw_num = row.get('student_number', '').strip()
                raw_name = row.get('name', '').strip().upper()
                
                if raw_num and raw_name:
                    five_digit = clean_student_number(raw_num)
                    entries.append((raw_name, f"🟢 `[ {raw_name} ]` `[ {five_digit} ]` ACTIVE"))
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
        fieldnames = list(reader.fieldnames) if reader.fieldnames else ['name', 'student_number', 'discord_id', 'alt_id_1', 'alt_id_2', 'password']
        
        for col in ['discord_id', 'alt_id_1', 'alt_id_2', 'password']:
            if col not in fieldnames: fieldnames.append(col)
            
        for row in reader:
            for col in ['discord_id', 'alt_id_1', 'alt_id_2', 'password']:
                if col not in row: row[col] = ''
                
            csv_num = row.get('student_number', '').strip()
            if csv_num == student_num.strip():
                found = True
                final_five_digit = clean_student_number(csv_num)
                final_name = row.get('name', '').strip().upper()
                
                if row.get('discord_id') and row['discord_id'].strip():
                    status = "already_verified"
                else:
                    row['discord_id'] = str(discord_id)
                    row['password'] = f"cpe-{final_five_digit}"  # Default password format set
                    status = "success"
            rows.append(row)
            
    if found and status == "success":
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
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
        fieldnames = list(reader.fieldnames)
        
        for row in reader:
            csv_num = row.get('student_number', '').strip()
            if csv_num == student_num.strip():
                # Backwards compatible check: If password column is blank, look for default format
                stored_pass = row.get('password', '').strip()
                if not stored_pass:
                    stored_pass = f"cpe-{final_five_digit}"
                
                if stored_pass != provided_pass.strip():
                    status = "wrong_password"
                    msg = "Invalid password credential provided."
                    rows.append(row)
                    continue
                
                str_id = str(discord_id)
                if row.get('discord_id') == str_id or row.get('alt_id_1') == str_id or row.get('alt_id_2') == str_id:
                    status = "already_linked"
                    msg = "This Discord account is already logged into this student record!"
                    rows.append(row)
                    continue

                if not row.get('alt_id_1') or not row['alt_id_1'].strip():
                    row['alt_id_1'] = str_id
                    status = "success"
                    msg = row.get('name', '').strip().upper()
                elif not row.get('alt_id_2') or not row['alt_id_2'].strip():
                    row['alt_id_2'] = str_id
                    status = "success"
                    msg = row.get('name', '').strip().upper()
                else:
                    status = "max_slots"
                    msg = "Limit reached: Maximum of 2 alternate accounts allowed."
            rows.append(row)

    if status == "success":
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return status, msg

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
        if channel: await channel.delete(reason="Ticket expired.")
    except: pass 

# --- TICKET UI PANELS --- 
class PersistentPanel(discord.ui.View): 
    def __init__(self): 
        super().__init__(timeout=None) 

    @discord.ui.button(label="🎫 Verify Account", style=discord.ButtonStyle.danger, custom_id="start_verification_btn") 
    async def start_verification(self, interaction: discord.Interaction, button: discord.ui.Button): 
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild 
        member = interaction.user 
        category = guild.get_channel(CATEGORY_ID) 
         
        if not category or not isinstance(category, discord.CategoryChannel): 
            await interaction.followup.send("❌ Setup configuration mapping mismatch.", ephemeral=True) 
            return 

        classmate_role = guild.get_role(ROLE_ID) 
        if classmate_role and classmate_role in member.roles: 
            await interaction.followup.send("⚠️ Your profile already has server access!", ephemeral=True) 
            return 

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
                "**First Time Registering?** Use:\n" 
                "➡️ `/verify [your_student_number]`\n" 
                "*(Your default login password will be `cpe-[your_5_digits]`)*\n\n" 
                "**Logging into a Dummy Account?** Use:\n" 
                "➡️ `/login student_number: password:`\n\n" 
                "⚠️ *This terminal dissolves automatically in 3 minutes if unverified.*"
            ), 
            color=discord.Color.from_rgb(231, 76, 60) 
        ) 
        await ticket_channel.send(embed=embed, content=member.mention) 
        await interaction.followup.send(f"✅ Secure workspace pipeline opened: {ticket_channel.mention}", ephemeral=True) 
        bot.loop.create_task(channel_lifespan_timer(ticket_channel.id)) 

    @discord.ui.button(label="🔑 Forgot Password", style=discord.ButtonStyle.secondary, custom_id="forgot_password_btn")
    async def forgot_password(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        category = guild.get_channel(CATEGORY_ID)
        
        overwrites = { 
            guild.default_role: discord.PermissionOverwrite(view_channel=False), 
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True), 
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True) 
        }
        
        reset_channel = await guild.create_text_channel(name=f"forgot-pass-{member.name}", category=category, overwrites=overwrites)
        
        embed = discord.Embed(
            title="🔐 Password Recovery Operations",
            description=f"Greetings {member.mention}.\n\nAn administrator has been notified to assist you with a manual account override. Please provide your **Full Name** and **Student Number** below.",
            color=discord.Color.orange()
        )
        await reset_channel.send(embed=embed, content=f"⚠️ Support Request for {member.mention}", view=AdminTicketView())
        await interaction.followup.send(f"🛠️ Assistance terminal deployed: {reset_channel.mention}", ephemeral=True)

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
    member = interaction.user 
    channel_id = interaction.channel.id 
    
    if channel_id not in bot.attempts_tracker: bot.attempts_tracker[channel_id] = 0 
    result, five_digit, full_name = check_and_verify_student(student_number, member.id) 
    
    if result == "not_found": 
        bot.attempts_tracker[channel_id] += 1 
        strikes = 3 - bot.attempts_tracker[channel_id] 
        if strikes <= 0: 
            await interaction.followup.send("❌ Out of attempts. Closing channel...", ephemeral=True) 
            await asyncio.sleep(5) 
            await interaction.channel.delete(reason="Excessive verification failures.") 
        else: 
            await interaction.followup.send(f"❌ Record mismatch. **{strikes}** attempts remain.", ephemeral=True) 
            
    elif result == "already_verified": 
        await interaction.followup.send("⚠️ Profile busy: A master user is already tied to this record.", ephemeral=True) 
        
    elif result == "success": 
        role = interaction.guild.get_role(ROLE_ID) 
        lurker = interaction.guild.get_role(LURKER_ROLE_ID) 
        if role: 
            await member.add_roles(role) 
            if lurker and lurker in member.roles: await member.remove_roles(lurker) 
            await interaction.followup.send(f"🎉 Welcome aboard **{full_name}**! Default password set to: `cpe-{five_digit}`. Channel closing in 5s...", ephemeral=True) 
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
    member = interaction.user
    res, message = process_dummy_login(student_number, password, member.id)
    
    if res in ["not_found", "wrong_password", "max_slots", "already_linked"]:
        await interaction.followup.send(f"❌ Authentication Rejected: {message}", ephemeral=True)
    elif res == "success":
        role = interaction.guild.get_role(ROLE_ID)
        lurker = interaction.guild.get_role(LURKER_ROLE_ID)
        if role:
            await member.add_roles(role)
            if lurker and lurker in member.roles: await member.remove_roles(lurker)
            await interaction.followup.send(f"🔓 Alt account linked successfully under identity **{message}**! Closing channel in 5s...", ephemeral=True)
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
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(student_number="Target student number", new_password="New password string")
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
    embed = discord.Embed( 
        title="📚 CPE 1-1 Section Gateway", 
        description="Welcome to the digital operations perimeter workspace. Accessing community tools requires verification.", 
        color=discord.Color.from_rgb(231, 76, 60)  
    ) 
    await ctx.send(embed=embed, view=PersistentPanel()) 
    try: await ctx.message.delete() 
    except: pass 

@bot.event 
async def on_ready(): 
    print(f"🤖 Operational Status: Connected as {bot.user}") 
    try: 
        synced = await bot.tree.sync() 
        print(f"🔄 Global Command Matrix Synced: {len(synced)} entries live.") 
        await update_verification_log_board() 
    except Exception as e: print(f"❌ Error during startup: {e}") 

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