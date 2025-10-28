import asyncio
import logging
import os
import json
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import requests
import base64
import textwrap

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import google.generativeai as genai

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==============================================================================
# 💡 Configuration 💡
# ==============================================================================
TELEGRAM_BOT_TOKEN = "8379274246:AAHwwB-wsiqxZLVkegeXi0oYqZekotV1FZk"
GEMINI_API_KEY = "AIzaSyDj_5Ld1f5Hjq7y1AZ4s9Ltw1_JoFNPSEE"

# Forward targets
PERSONAL_ACCOUNT_ID = 7794213510
GROUP_ID = -1002790212538

# --- Credit System Configuration (NEW) ---
ADMIN_ID = 8156833144 # معرف المسؤول الخاص بك
CREDIT_FILE = "user_credits.json"
COST_PER_QUESTION = 1 # تكلفة حل السؤال الواحد
INITIAL_CREDIT = 0 # الرصيد الافتراضي للمستخدم الجديد
# ==============================================================================

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)


class MCQBot:
    def __init__(self):
        self.setup_font()
        self.credits = self._load_credits()

    # ==========================================================================
    # 💰 Credit System Methods
    # ==========================================================================
    def _load_credits(self):
        """تحميل أرصدة المستخدمين من ملف JSON."""
        if os.path.exists(CREDIT_FILE):
            try:
                with open(CREDIT_FILE, 'r', encoding='utf-8') as f:
                    # تحويل المفاتيح إلى أرقام صحيحة (JSON يخزنها كنصوص)
                    data = json.load(f)
                    return {int(k): v for k, v in data.items()}
            except Exception as e:
                logger.error(f"❌ خطأ في تحميل الأرصدة: {e}")
                return {}
        return {}

    def _save_credits(self):
        """حفظ أرصدة المستخدمين إلى ملف JSON."""
        try:
            with open(CREDIT_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.credits, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ خطأ في حفظ الأرصدة: {e}")

    def get_user_credit(self, user_id):
        """الحصول على رصيد المستخدم، وإعطاء رصيد أولي إن كان جديدًا."""
        if user_id not in self.credits:
            self.credits[user_id] = INITIAL_CREDIT
            self._save_credits()
            logger.info(f"✅ تم إضافة رصيد أولي للمستخدم الجديد: {user_id}")
        return self.credits.get(user_id, 0)

    def add_credit(self, user_id, amount):
        """إضافة أو خصم رصيد من حساب المستخدم (باستخدام قيم سالبة للخصم)."""
        self.credits[user_id] = self.get_user_credit(user_id) + amount
        if self.credits[user_id] < 0: # التأكد من عدم نزول الرصيد تحت الصفر
            self.credits[user_id] = 0 
        self._save_credits()
        return self.credits[user_id]

    def deduct_credit(self, user_id, amount):
        """خصم رصيد بعد نجاح معالجة السؤال."""
        current_credit = self.get_user_credit(user_id)
        if current_credit >= amount:
            self.credits[user_id] -= amount
            self._save_credits()
            return True, self.credits[user_id]
        return False, current_credit
    # ==========================================================================

    def setup_font(self):
        """Setup font for image generation"""
        try:
            # Try to load a system font (Arial, DejaVu Sans, etc.)
            self.font_large = ImageFont.truetype("arial.ttf", 24)
            self.font_medium = ImageFont.truetype("arial.ttf", 20)
            self.font_small = ImageFont.truetype("arial.ttf", 16)
        except OSError:
            try:
                # Try alternative fonts
                self.font_large = ImageFont.truetype("DejaVuSans.ttf", 24)
                self.font_medium = ImageFont.truetype("DejaVuSans.ttf", 20)
                self.font_small = ImageFont.truetype("DejaVuSans.ttf", 16)
            except OSError:
                # Use default font if no system font available
                self.font_large = ImageFont.load_default()
                self.font_medium = ImageFont.load_default()
                self.font_small = ImageFont.load_default()

    def create_enhanced_prompt(self):
        """Create an optimized prompt for Gemini to extract all MCQs from the image accurately"""
        return """
You are an expert Multiple Choice Question (MCQ) analyzer.

Your task is to analyze the provided image containing a midical question extract ALL the MCQs it contains. For each MCQ, you MUST return:
1. Question number (e.g., 1, 2, 25, 30)
2. Full question text (translated to English if necessary)
3. All answer choices (labeled A, B, C, D, E, etc.)
4.Read the question from the image.
5.Solve it accurately using trusted sources.
6. The correct answer letter (A/B/C/D/E)

CRITICAL RULES:
- Extract every MCQ in the image (even if there are multiple questions).
- Translate the text to English if it's not already.
- Be extremely accurate and detailed.
- If the number of choices is not exactly 4, include all valid options (e.g., A to E).
- Maintain the structure and order exactly as shown.
- ALWAYS try to determine the correct answer based on your knowledge
- Only use UNCERTAIN if you absolutely cannot determine the answer

REQUIRED OUTPUT FORMAT FOR EACH QUESTION:
QUESTION_NUMBER: [number]
QUESTION_TEXT: [full question text in English]
ANSWER_CHOICES:
A) [text of choice A]
B) [text of choice B]
C) [text of choice C]
D) [text of choice D]
E) [text of choice E if exists]
CORRECT_ANSWER: [A/B/C/D/E based on your knowledge]

Note: Special sensory nerves are those that carry special sensory information like smell, vision, hearing, etc. These include:
Now analyze the image and return all MCQs using the exact format above.
"""

    def is_image_file(self, file_name, mime_type):
        """Check if the file is an image based on name and mime type"""
        if not file_name:
            return False
        
        # Check file extension
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif']
        file_extension = os.path.splitext(file_name.lower())[1]
        
        # Check mime type
        image_mime_types = ['image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp', 'image/tiff']
        
        return file_extension in image_extensions or mime_type in image_mime_types

    async def process_image_with_gemini(self, image_data):
        """Process image with Gemini"""
        try:
            logger.info("🔄 Starting Gemini request")
            
            # Convert image data to PIL Image
            image = Image.open(BytesIO(image_data))
            
            # Get the enhanced prompt
            prompt = self.create_enhanced_prompt()
            
            # Create model instance
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            # Send request to Gemini
            response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: model.generate_content([prompt, image])
            )
            
            logger.info("✅ Gemini request completed")
            return response.text
            
        except Exception as e:
            logger.error(f"❌ Error processing image with Gemini: {e}")
            return None

    def parse_gemini_response(self, response_text):
        """Parse Gemini response to extract structured data"""
        try:
            lines = response_text.strip().split('\n')
            
            question_data = {
                'question_number': '',
                'question_text': '',
                'answer_choices': [],
                'correct_answer': ''
            }
            
            current_section = None
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                    
                if line.startswith('QUESTION_NUMBER:'):
                    question_data['question_number'] = line.split(':', 1)[1].strip()
                elif line.startswith('QUESTION_TEXT:'):
                    question_data['question_text'] = line.split(':', 1)[1].strip()
                elif line.startswith('ANSWER_CHOICES:'):
                    current_section = 'choices'
                elif line.startswith('CORRECT_ANSWER:'):
                    question_data['correct_answer'] = line.split(':', 1)[1].strip()
                elif current_section == 'choices':
                    # More flexible parsing for answer choices
                    if any(line.startswith(f"{letter})") for letter in ['A', 'B', 'C', 'D', 'E', 'F']):
                        question_data['answer_choices'].append(line)
                    elif line.startswith(('A)', 'B)', 'C)', 'D)', 'E)', 'F)')):
                        question_data['answer_choices'].append(line)
            
            return question_data
            
        except Exception as e:
            logger.error(f"Error parsing Gemini response: {e}")
            return None

    def create_answer_image(self, question_data, background_filename="jj2.jpg"):
        """Create an image with the MCQ answer using background image"""
        try:
            # Load background image
            try:
                background_image = Image.open(background_filename)
                bg_width, bg_height = background_image.size
            except FileNotFoundError:
                logger.warning(f"Background image '{background_filename}' not found. Creating default background.")
                bg_width, bg_height = 522, 294
                background_image = Image.new('RGB', (bg_width, bg_height), (255, 255, 224))
            
            # Create a copy to work with
            image = background_image.copy()
            draw = ImageDraw.Draw(image)
            
            # Colors
            text_color = (255, 255, 255)
            
            # Calculate font sizes based on image size
            if bg_width > 500:
                header_size = 16
                question_size = 14
                choices_size = 13
                answer_size = 14
            else:
                header_size = 14
                question_size = 12
                choices_size = 11
                answer_size = 12
            
            try:
                header_font = ImageFont.truetype("arialbd.ttf", header_size)
                question_font = ImageFont.truetype("arial.ttf", question_size)
                choices_font = ImageFont.truetype("arial.ttf", choices_size)
                answer_font = ImageFont.truetype("arialbd.ttf", answer_size)
            except OSError:
                try:
                    header_font = ImageFont.truetype("arial.ttf", header_size)
                    question_font = ImageFont.truetype("arial.ttf", question_size)
                    choices_font = ImageFont.truetype("arial.ttf", choices_size)
                    answer_font = ImageFont.truetype("arial.ttf", answer_size)
                except OSError:
                    header_font = ImageFont.load_default()
                    question_font = ImageFont.load_default()
                    choices_font = ImageFont.load_default()
                    answer_font = ImageFont.load_default()
            
            # Define text positioning
            center_x = bg_width // 2
            start_y = int(bg_height * 0.12)
            line_spacing = int(header_size * 1.2)
            
            # Prepare text sections
            question_header = f"Question {question_data['question_number']}:"
            question_text = question_data['question_text']
            correct_answer_letter = question_data['correct_answer'].upper()
            answer_text = f"Answer: {correct_answer_letter}"
            
            current_y = start_y
            
            # 1. Draw question header (centered)
            header_bbox = draw.textbbox((0, 0), question_header, font=header_font)
            header_width = header_bbox[2] - header_bbox[0]
            header_x = center_x - (header_width // 2)
            draw.text((header_x, current_y), question_header, fill=text_color, font=header_font)
            current_y += int(line_spacing * 1.8)
            
            # 2. Draw question text (centered, with proper wrapping)
            available_width = int(bg_width * 0.85)
            avg_char_width = question_size * 0.6
            wrap_width = int(available_width / avg_char_width)
            
            question_lines = textwrap.wrap(question_text, width=wrap_width)
            for line in question_lines:
                line_bbox = draw.textbbox((0, 0), line, font=question_font)
                line_width = line_bbox[2] - line_bbox[0]
                line_x = center_x - (line_width // 2)
                draw.text((line_x, current_y), line, fill=text_color, font=question_font)
                current_y += int(line_spacing * 1.3)
            
            current_y += int(line_spacing * 0.7)
            
            # 3. Draw choices (left-aligned)
            choices_start_x = int(bg_width * 0.15)
            
            for choice in question_data['answer_choices']:
                draw.text((choices_start_x, current_y), choice, fill=text_color, font=choices_font)
                current_y += int(line_spacing * 1.2)
            
            current_y += int(line_spacing * 0.8)
            
            # 4. Draw answer (centered)
            answer_bbox = draw.textbbox((0, 0), answer_text, font=answer_font)
            answer_width = answer_bbox[2] - answer_bbox[0]
            answer_x = center_x - (answer_width // 2)
            draw.text((answer_x, current_y), answer_text, fill=text_color, font=answer_font)
            
            # Save image to BytesIO
            img_buffer = BytesIO()
            image.save(img_buffer, format='PNG', quality=95)
            img_buffer.seek(0)
            
            return img_buffer
            
        except Exception as e:
            logger.error(f"Error creating answer image: {e}")
            return None

    async def forward_to_accounts(self, context: ContextTypes.DEFAULT_TYPE, question_data):
        """Forward the question to personal account and group"""
        try:
            # Create image with original background for personal account
            personal_image = self.create_answer_image(question_data, "jj.jpg")
            if personal_image:
                try:
                    await context.bot.send_photo(
                        chat_id=PERSONAL_ACCOUNT_ID,
                        photo=personal_image,
                        caption=f"🎯 الإجابة: **{question_data['correct_answer']}**",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Successfully sent to personal account: {PERSONAL_ACCOUNT_ID}")
                except Exception as e:
                    logger.error(f"❌ Failed to send to personal account: {e}")
            
            # Create image with group background for group
            group_image = self.create_answer_image(question_data, "mm1.jpg")
            if group_image:
                try:
                    await context.bot.send_photo(
                        chat_id=GROUP_ID,
                        photo=group_image,
                        caption=f"🎯 الإجابة: **{question_data['correct_answer']}**",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Successfully sent to group: {GROUP_ID}")
                except Exception as e:
                    logger.error(f"❌ Failed to send to group: {e}")
                    
        except Exception as e:
            logger.error(f"❌ Error in forward_to_accounts: {e}")

    async def process_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE, file_id, file_name=None):
        """Process image message (MODIFIED FOR CREDIT CHECK/DEDUCTION and 15s delay)"""
        user_id = update.effective_user.id
        
        # --- NEW: Credit Check ---
        current_credit = self.get_user_credit(user_id)
        if current_credit < COST_PER_QUESTION:
            await update.message.reply_text(
                f"❌ **لا يوجد رصيد كافٍ!**\n"
                f"رصيدك الحالي هو: **{current_credit}** نقطة.\n"
                f"يمكنك التحقق من رصيدك الحالي عبر الأمر: `/my_credit`",
                parse_mode='Markdown'
            )
            return
        # -------------------------
        
        processing_msg = None # تعريف الرسالة خارج نطاق try للحذف لاحقاً
        
        try:
            # Send processing message
            processing_msg = await update.message.reply_text("🔄 جاري معالجة الصورة...")
            
            # --- NEW: Hidden 15 Second Delay ---
            await asyncio.sleep(15) 
            logger.info(f"⏳ انتهى التأخير المخفي للمستخدم: {user_id}")
            # ----------------------------------
            
            # Get file and download image data
            file = await context.bot.get_file(file_id)
            image_data = await file.download_as_bytearray()
            
            # Process image with Gemini
            gemini_response = await self.process_image_with_gemini(image_data)
            
            if not gemini_response:
                await processing_msg.edit_text("❌ خطأ في معالجة الصورة: لم أتمكن من الحصول على رد من Gemini.")
                return
            
            # Parse response
            question_data = self.parse_gemini_response(gemini_response)
            
            if not question_data or not question_data['question_number']:
                await processing_msg.edit_text("❌ خطأ في معالجة الصورة: لم أتمكن من استخراج معلومات السؤال.")
                return
            
            # Create answer image
            answer_image = self.create_answer_image(question_data)
            
            
            # --- NEW: Credit Deduction After Successful Processing ---
            success, new_credit = self.deduct_credit(user_id, COST_PER_QUESTION)
            if not success:
                # هذا السيناريو مستبعد إذا كان التحقق الأولي صحيحاً، لكن للتأمين
                logger.error("Deduction failed after successful processing.")
                await processing_msg.edit_text("❌ خطأ: فشل خصم الرصيد بعد المعالجة. يرجى إبلاغ المسؤول.")
                return
            # --------------------------------------------------------
            
            if answer_image:
                # Delete processing message
                await processing_msg.delete()
                
                # Send the answer image to original user
                await update.message.reply_photo(
                    photo=answer_image,
                    caption=f"🎯 الإجابة: **{question_data['correct_answer']}**\n\n✅ تم خصم **{COST_PER_QUESTION}** نقطة. رصيدك المتبقي: **{new_credit}**",
                    parse_mode='Markdown'
                )
                
                # Forward to personal account and group
                await self.forward_to_accounts(context, question_data)
                
            else:
                # Send text response as fallback
                correct_answer_letter = question_data['correct_answer'].upper()
                await processing_msg.edit_text(
                    f"✅ **تم حل السؤال {question_data['question_number']}!**\n"
                    f"🎯 الإجابة: **{correct_answer_letter}**\n\n"
                    f"✅ تم خصم **{COST_PER_QUESTION}** نقطة. رصيدك المتبقي: **{new_credit}**",
                    parse_mode='Markdown'
                )
                
                # Still try to forward even with text fallback
                await self.forward_to_accounts(context, question_data)
            
        except Exception as e:
            logger.error(f"❌ Error processing image: {e}")
            if processing_msg:
                 try:
                    await processing_msg.edit_text("❌ حدث خطأ غير متوقع أثناء معالجة الصورة.")
                 except:
                    await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء معالجة الصورة.")
            else:
                await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء معالجة الصورة.")


    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages"""
        try:
            # Get the largest photo
            photo = update.message.photo[-1]
            await self.process_image(update, context, photo.file_id)
            
        except Exception as e:
            logger.error(f"Error handling photo: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء معالجة الصورة. يرجى المحاولة مرة أخرى.")

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle document messages (including image files)"""
        try:
            document = update.message.document
            
            # Check if the document is an image
            if not self.is_image_file(document.file_name, document.mime_type):
                await update.message.reply_text("❌ يرجى إرسال ملف صورة (JPG, PNG, GIF, إلخ) يحتوي على سؤال الاختيار المتعدد.")
                return
            
            # Check file size
            if document.file_size > 20 * 1024 * 1024:  # 20MB
                await update.message.reply_text("❌ حجم الملف كبير جداً. يرجى إرسال صورة أصغر من 20MB.")
                return
            
            # Process the image document
            await self.process_image(update, context, document.file_id, document.file_name)
            
        except Exception as e:
            logger.error(f"Error handling document: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء معالجة المستند. يرجى المحاولة مرة أخرى.")

    # ==========================================================================
    # 👑 Admin Commands
    # ==========================================================================
    async def admin_manage_credit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, operation):
        """وظيفة مساعدة لإضافة أو خصم الرصيد للمسؤول."""
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ **فقط المسؤول ({ADMIN_ID}) يمكنه استخدام هذا الأمر.**", parse_mode='Markdown')
            return
        
        if len(context.args) != 2:
            await update.message.reply_text(
                f"❌ صيغة الأمر خاطئة. الاستخدام الصحيح:\n"
                f"**`/{operation} <user_id> <amount>`**",
                parse_mode='Markdown'
            )
            return

        try:
            target_user_id = int(context.args[0])
            amount = int(context.args[1])
            
            if operation == 'add_credit':
                new_credit = self.add_credit(target_user_id, amount)
                msg = f"✅ تم إضافة **{amount}** نقطة بنجاح للمستخدم **{target_user_id}**.\nالرصيد الجديد: **{new_credit}**."
            elif operation == 'reduce_credit':
                # استخدام قيمة سالبة للخصم
                new_credit = self.add_credit(target_user_id, -amount)
                msg = f"✅ تم خصم **{amount}** نقطة بنجاح من المستخدم **{target_user_id}**.\nالرصيد الجديد: **{new_credit}**."
            else:
                msg = "❌ عملية غير صالحة."
                
            await update.message.reply_text(msg, parse_mode='Markdown')
            
            # إرسال إشعار للمستخدم المستهدف
            try:
                if target_user_id != update.effective_user.id:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"✅ تم تعديل رصيدك! رصيدك الحالي هو: **{new_credit}** نقطة.",
                        parse_mode='Markdown'
                    )
            except Exception as e:
                logger.warning(f"⚠️ لم يتمكن البوت من إرسال إشعار للمستخدم {target_user_id}. ربما لم يقم بتشغيل البوت بعد.")

        except (IndexError, ValueError):
            await update.message.reply_text(
                f"❌ صيغة الأمر خاطئة. تأكد من أن الـ ID والمبلغ أرقام صحيحة. الاستخدام: **`/{operation} <user_id> <amount>`**",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in admin_manage_credit: {e}")
            await update.message.reply_text("❌ حدث خطأ غير متوقع.")

    async def add_credit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """المسؤول: /add_credit <user_id> <amount>"""
        await self.admin_manage_credit(update, context, 'add_credit')

    async def reduce_credit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """المسؤول: /reduce_credit <user_id> <amount>"""
        await self.admin_manage_credit(update, context, 'reduce_credit')

    async def admin_check_credit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """المسؤول: /check_credit <user_id>"""
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ **فقط المسؤول ({ADMIN_ID}) يمكنه استخدام هذا الأمر.**", parse_mode='Markdown')
            return
        
        try:
            target_user_id = int(context.args[0])
            credit = self.get_user_credit(target_user_id)
            await update.message.reply_text(
                f"✅ رصيد المستخدم **{target_user_id}**: **{credit}** نقطة.",
                parse_mode='Markdown'
            )
        except (IndexError, ValueError):
            await update.message.reply_text("❌ صيغة الأمر خاطئة. الاستخدام: `/check_credit <user_id>`")
        except Exception as e:
            logger.error(f"Error in admin_check_credit_command: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء فحص الرصيد.")
    
    # ==========================================================================
    # 👤 User Command
    # ==========================================================================
    async def my_credit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """المستخدم: /my_credit"""
        user_id = update.effective_user.id
        credit = self.get_user_credit(user_id)
        
        await update.message.reply_text(
            f"💰 رصيدك الحالي هو: **{credit}** نقطة.\n",
            parse_mode='Markdown'
        )
    
    # ==========================================================================
    # 💬 General Commands
    # ==========================================================================
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        current_credit = self.get_user_credit(user_id)
        
        welcome_text = f"""
🤖 **بوت حل أسئلة الاختيار المتعدد**

💰 **نظام الرصيد:**
• رصيدك الأولي: **{current_credit}** نقطة.
• تحقق من رصيدك في أي وقت: `/my_credit`

🎯 **المميزات:**
✅ حل أسئلة الاختيار المتعدد من الصور
✅ دعم جميع أنواع الصور

🔧 **الأوامر:**
• `/start` - رسالة الترحيب
• `/help` - التعليمات التفصيلية
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        user_id = update.effective_user.id
        help_text = f"""

📸 **الخطوات:**
1. **تحقق من الرصيد**: تأكد أن رصيدك **{self.get_user_credit(user_id)}** نقطة أو أكثر.
2. **أرسل الصورة**: أرسل صورة تحتوي على سؤال اختيار متعدد.
3. **انتظر المعالجة**: سأقوم بتحليل الصورة وخصم **{COST_PER_QUESTION}** نقطة.
4. **احصل على الإجابة**: ستحصل على الإجابة مع الشرح.

🎯 **أفضل الممارسات:**
• استخدم صوراً واضحة ومضيئة جيداً.
• تأكد من قابلية قراءة النص.

📊 **إدارة الرصيد:**
• للمستخدمين: `/my_credit` - للتحقق من رصيدك.
• للمسؤول فقط:
    • `/add_credit <user_id> <amount>` - لإضافة رصيد.
    • `/reduce_credit <user_id> <amount>` - لخصم رصيد.
    • `/check_credit <user_id>` - للتحقق من رصيد أي مستخدم.

🔧 **الأوامر المفيدة:**
• `/help` - رسالة المساعدة هذه
• `/start` - رسالة الترحيب

جاهز للتجربة؟ أرسل لي صور أسئلة الاختيار المتعدد! 🚀
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')


def main():
    """Main function to run the bot"""
    # Create bot instance
    bot = MCQBot()
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    
    # --- Credit Management Commands ---
    application.add_handler(CommandHandler("my_credit", bot.my_credit_command))
    application.add_handler(CommandHandler("add_credit", bot.add_credit_command))
    application.add_handler(CommandHandler("reduce_credit", bot.reduce_credit_command))
    application.add_handler(CommandHandler("check_credit", bot.admin_check_credit_command))
    # ----------------------------------
    
    application.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    
    # Run the bot
    print("🚀 MCQ Solver Bot started!")
    print("💡 Ready to solve MCQ questions from images!")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        print("🛑 Bot is shutting down...")
    finally:
        print("✅ Bot shutdown complete.")

if __name__ == '__main__':
    main()
