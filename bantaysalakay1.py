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
        # 1. Register your persistent buttons view
        self.add_view(PersistentPanel())
        print("🔒 Persistent View Registered")
        
        # 2. Spin up the keep-alive web server safely right here
        print("🌐 Starting background keep-alive web server...")
        threading.Thread(target=run_web_server, daemon=True).start()

bot = VerificationBot()

# --- BACKEND LOGIC ---
def clean_student_number(student_num: str) -> str:
    """Extracts just the middle 5 digits from formats like '2025-00602-BN-0'"""
    parts = student_num.strip().split('-')
    if len(parts) >= 2:
        return parts[1] 
    return student_num.strip()[-5:] 

def get_all_verified_entries() -> list[str]:
    """Reads the CSV and returns an alphabetical list of verified members with full names"""
    if not os.path.exists(CSV_FILE):
        return []
    
    entries = []
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('discord_id') and row['discord_id'].strip():
                raw_num = row.get('student_number', '').strip()
                raw_name = row.get('name', '').strip().upper()
                
                if raw_num and raw_name:
                    five_digit = clean_student_number(raw_num)
                    entries.append((raw_name, f"🟢 `[ {raw_name} ]` `[ {five_digit} ]` VERIFIED"))
                    
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
        fieldnames = list(reader.fieldnames) if reader.fieldnames else ['name', 'student_number', 'discord_id']
        
        if 'discord_id' not in fieldnames:
            fieldnames.append('discord_id')
            
        for row in reader:
            if 'discord_id' not in row:
                row['discord_id'] = ''
                
            csv_num = row.get('student_number', '').strip()
            provided_num = student_num.strip()
            
            if csv_num == provided_num:
                found = True
                final_five_digit = clean_student_number(csv_num)
                final_name = row.get('name', '').strip().upper()
                
                if row.get('discord_id') and row['discord_id'].strip():
                    status = "already_verified"
                else:
                    row['discord_id'] = str(discord_id)
                    status = "success"
            rows.append(row)
            
    if found and status == "success":
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            
    return status, final_five_digit, final_name

async def update_verification_log_board():
    """Finds or creates a permanent verification tracking roster embed inside the log channel"""
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        print("❌ Error: Log channel not found.")
        return

    verified_lines = get_all_verified_entries()
    description_text = "\n".join(verified_lines) if verified_lines else "*No verified classmates registered yet.*"

    embed = discord.Embed(
        title="📋 CPE 1-1 Official Verification Roster Status",
        description=description_text,
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Total Verified Classmates: {len(verified_lines)}")

    message_found = False
    try:
        async for msg in log_channel.history(limit=50):
            if msg.author == bot.user and msg.embeds and "Official Verification Roster Status" in msg.embeds[0].title:
                await msg.edit(embed=embed)
                message_found = True
                break
        
        if not message_found:
            await log_channel.send(embed=embed)
    except Exception as e:
        print(f"❌ Failed to refresh status tracking board: {e}")

async def channel_lifespan_timer(channel_id: int):
    """Background task that dissolves the verification channel after 3 minutes if unverified"""
    await asyncio.sleep(180)
    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)
        if channel:
            await channel.delete(reason="Ticket expired: 3-minute time limit reached.")
    except discord.NotFound:
        pass 
    except Exception as e:
        print(f"⚠️ Error during channel auto-delete: {e}")

# --- TICKET UI PANELS ---
class PersistentPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 Verify Identity", 
        style=discord.ButtonStyle.danger, 
        custom_id="start_verification_btn"
    )
    async def start_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        category = guild.get_channel(CATEGORY_ID)
        
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("❌ Configuration Error: Category not found.", ephemeral=True)
            return

        classmate_role = guild.get_role(ROLE_ID)
        if classmate_role and classmate_role in member.roles:
            await interaction.response.send_message("⚠️ You are already verified as a classmate!", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"verify-{member.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Classmate verification portal for {member.id}"
        )

        embed = discord.Embed(
            title="🏫 CPE 1-1 Verification Portal",
            description=(
                f"Welcome {member.mention}!\n\n"
                "To gain access to our class section workspace, use the slash command below:\n"
                "➡️ `/verify [your_student_number]`\n\n"
                "⚠️ **Important Rules:**\n"
                "* This channel will automatically close in **3 minutes** if unverified.\n"
                "* Entering an invalid student number **3 times** will auto-delete this terminal."
            ),
            color=discord.Color.from_rgb(231, 76, 60)
        )
        if bot.user.display_avatar:
            embed.set_footer(text="Powered by Bantay Salakay", icon_url=bot.user.display_avatar.url)
        
        await ticket_channel.send(embed=embed, content=member.mention)
        await interaction.response.send_message(f"✅ Private verification portal opened: {ticket_channel.mention}", ephemeral=True)
        
        bot.loop.create_task(channel_lifespan_timer(ticket_channel.id))


# --- SLASH COMMAND ENGINE ---
@bot.tree.command(name="verify", description="Verify your identity using your student number inside your open channel.")
@app_commands.describe(student_number="Enter your official student number")
async def verify(interaction: discord.Interaction, student_number: str):
    if not interaction.channel.name.startswith("verify-"):
        await interaction.response.send_message("❌ This command can only be processed inside your private verification channel panel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    member = interaction.user
    guild = interaction.guild
    channel_id = interaction.channel.id
    
    if channel_id not in bot.attempts_tracker:
        bot.attempts_tracker[channel_id] = 0
        
    result, five_digit, full_name = check_and_verify_student(student_number, member.id)
    
    if result == "not_found":
        bot.attempts_tracker[channel_id] += 1
        remaining_strikes = 3 - bot.attempts_tracker[channel_id]
        
        if remaining_strikes <= 0:
            await interaction.followup.send("❌ Out of attempts! You have failed 3 times. Closing terminal in 5 seconds...", ephemeral=True)
            if channel_id in bot.attempts_tracker:
                del bot.attempts_tracker[channel_id]
            await asyncio.sleep(5)
            await interaction.channel.delete(reason="Failed verification 3 times.")
        else:
            await interaction.followup.send(f"❌ Student number not matched in our roster. You have **{remaining_strikes}** attempts remaining.", ephemeral=True)
            
    elif result == "already_verified":
        await interaction.followup.send("⚠️ This student identity has already been registered to an account. Closing terminal in 5 seconds...", ephemeral=True)
        if channel_id in bot.attempts_tracker:
            del bot.attempts_tracker[channel_id]
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Verification terminated: Identity registration duplication.")
        
    elif result == "success":
        if channel_id in bot.attempts_tracker:
            del bot.attempts_tracker[channel_id]
            
        role = guild.get_role(ROLE_ID)
        lurker_role = guild.get_role(LURKER_ROLE_ID)
        
        if role:
            try:
                await member.add_roles(role)
                if lurker_role and lurker_role in member.roles:
                    await member.remove_roles(lurker_role)
                
                await interaction.followup.send("✅ Identity verified! This portal channel will now self-destruct in 5 seconds...", ephemeral=True)
                await update_verification_log_board()

                await asyncio.sleep(5)
                await interaction.channel.delete(reason="Verification workflow completed successfully.")
                
            except discord.Forbidden:
                await interaction.followup.send("❌ Permissions Failure: Check bot hierarchy position.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Operational Error: Role tracking index misconfigured.", ephemeral=True)


# --- ADMIN SETUP UTILITIES ---
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_panel(ctx):
    """Admin tool to deploy the main ticket interface panel cleanly"""
    embed = discord.Embed(
        title="📚 CPE 1-1 Section Gate",
        description="Welcome to the official section workspace! Click the button below to open a private ticket and confirm your student ID roster information.",
        color=discord.Color.from_rgb(231, 76, 60) 
    )
    if bot.user.display_avatar:
        embed.set_footer(text="Powered by Bantay Salakay", icon_url=bot.user.display_avatar.url)
    await ctx.send(embed=embed, view=PersistentPanel())
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

@bot.event
async def on_ready():
    print(f"🤖 Operational Status: Connected as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Global Command Matrix Synced: {len(synced)} entries live.")
        
        print("📊 Synchronizing list dashboard status rosters...")
        await update_verification_log_board()
        print("✅ Status board sync complete!")
    except Exception as e:
        print(f"❌ Error during startup: {e}")


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

    def log_message(self, format, *args):
        return  # Suppress default logs to keep your terminal console clean

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), KeepAliveHandler)
    server.serve_forever()


# Launch Bot (The threading launch has moved inside setup_hook above!)
bot.run(TOKEN)