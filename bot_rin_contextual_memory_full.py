import os
import discord
import pandas as pd
from discord.ext import commands, tasks
from dotenv import load_dotenv
import random
from datetime import datetime
import anthropic  # 클로드 API 클라이언트
import logging  # 로깅 추가
import time  # 재시도 메커니즘을 위한 타임아웃

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('린_봇')

# 환경변수 로드
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")  # API 키 환경 변수 이름 변경

# API 키 검증
if not CLAUDE_API_KEY:
    logger.critical("CLAUDE_API_KEY 환경 변수가 설정되지 않았습니다. 프로그램을 종료합니다.")
    raise ValueError("CLAUDE_API_KEY 환경 변수가 필요합니다.")

if not DISCORD_BOT_TOKEN:
    logger.critical("DISCORD_BOT_TOKEN 환경 변수가 설정되지 않았습니다. 프로그램을 종료합니다.")
    raise ValueError("DISCORD_BOT_TOKEN 환경 변수가 필요합니다.")

# 클로드 API 클라이언트 초기화
try:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    logger.info("Claude API 클라이언트 초기화 성공")
except Exception as e:
    logger.critical(f"Claude API 클라이언트 초기화 실패: {str(e)}")
    raise

# 전역 캐시 및 대화 이력
recent_responses = []
conversation_history = []
last_message_time = {}
last_bot_message = {}

def is_redundant_response(reply):
    return any(reply[:10] in r for r in recent_responses)

def update_response_cache(reply):
    recent_responses.append(reply)
    if len(recent_responses) > 10:
        recent_responses.pop(0)

# 확장된 유사 표현 사전
extended_replace_map = {
    "바보 같아도": ["어설퍼도", "엉뚱해도", "허술해 보여도", "얼굴이 귀엽게 보일 정도로 엉망이어도"],
    "그 말": ["네 말", "방금 한 말", "너의 말", "그 얘기"],
    "부끄럽잖아": ["얼굴이 뜨거워지잖아", "민망하잖아", "쑥스러워지잖아", "부끄럽게 만들어"],
    "진짜,": ["정말로,", "솔직히,", "진심으로,", "있잖아아,"],
    "흥,": ["후훗,", "그래도,", "쳇,", "흐흥!,"]
}

def replace_repetitive_phrases(text):
    for original, variants in extended_replace_map.items():
        if original in text:
            # 대체 표현 중 하나를 무작위 선택하여 치환
            text = text.replace(original, random.choice(variants))
    return text

# 캐릭터 데이터 로드: system_prompt와 대사 DB 모두 포함
def load_character_data():
    try:
        df_prompt = pd.read_excel("character_table_flowtagged.xlsx", sheet_name="system_prompt_린")
        # 'lines_린' 시트는 여자친구 모드 대사(반응형, is_initiator == False)와 린이 먼저 말 거는 대사( is_initiator == True) 모두 포함
        df_lines_initiator = pd.read_excel("character_table_flowtagged.xlsx", sheet_name="lines_린")
        df_lines_reactive = pd.read_excel("girlfriend_mode_reactive_200.xlsx", sheet_name="lines_린")
        df_lines = pd.concat([df_lines_initiator, df_lines_reactive], ignore_index=True)
        system_prompt = df_prompt.iloc[0]["프롬프트"]
        logger.info("캐릭터 데이터 로드 성공")
        return df_prompt, df_lines, system_prompt
    except Exception as e:
        logger.error(f"캐릭터 데이터 로드 오류: {str(e)}")
        # 기본값 반환
        return pd.DataFrame(), pd.DataFrame(), "나는 린, 여자친구 역할을 하는 AI 어시스턴트야."

df_prompt, df_lines, system_prompt = load_character_data()
emotion_cache = {}

def classify_emotion_with_cache(message):
    """
    메시지의 감정을 분석하여 분류합니다. 캐시를 통해 이미 분석한 메시지는 재활용합니다.
    """
    try:
        if message in emotion_cache:
            return emotion_cache[message]

        text = message.lower()
        emotion = "조심스러움"  # 기본값

        emotion_keywords = {
            "기쁨": ["안녕", "하이", "반가워", "좋은 아침", "잘 잤어", "웃어", "기쁘다", "좋아"],
            "애정": ["사랑해", "좋아해", "보고 싶어", "너밖에 없어", "함께 있고 싶어"],
            "설렘": ["두근", "설레", "떨려", "긴장"],
            "감동": ["고마워", "감사", "감동"],
        }

        for emo, keywords in emotion_keywords.items():
            if any(kw in text for kw in keywords):
                emotion = emo
                break

        emotion_cache[message] = emotion
        return emotion
    except Exception as e:
        logger.error(f"감정 분류 중 오류: {str(e)}")
        return "조심스러움"  # 오류 발생 시 기본값 반환

def analyze_emotion_level(text):
    """
    텍스트의 감정 강도를 분석합니다.
    """
    try:
        text = text.lower()
        high_keywords = ["사랑", "좋아해", "고백", "소중한", "널 좋아해"]
        medium_keywords = ["보고 싶어", "기다렸어", "설레", "감동", "그리워"]
        if any(word in text for word in high_keywords):
            return "very_high"
        elif any(word in text for word in medium_keywords):
            return "high"
        elif "?" in text:
            return "low"
        else:
            return "very_low"
    except Exception as e:
        logger.error(f"감정 수준 분석 중 오류: {str(e)}")
        return "very_low"  # 오류 발생 시 기본값 반환

def classify_situation(line):
    """
    텍스트의 상황을 분류합니다.
    """
    try:
        text = str(line).lower()
        if any(kw in text for kw in ["안녕", "하이", "좋은 아침", "잘 잤어", "반가워"]):
            return "인사"
        elif any(kw in text for kw in ["사랑", "고백", "좋아해", "보고 싶어"]):
            return "애정 표현"
        elif any(kw in text for kw in ["생일", "축하", "기념일", "선물"]):
            return "기념일/축하"
        elif any(kw in text for kw in ["왜", "무슨", "뭐야", "언제"]):
            return "질문 응답"
        else:
            return "일반"
    except Exception as e:
        logger.error(f"상황 분류 중 오류: {str(e)}")
        return "일반"  # 오류 발생 시 기본값 반환

def guess_user_flow(message):
    """
    사용자 메시지의 의도와 흐름을 분석합니다.
    """
    try:
        text = message.lower()

        flow_keywords = {
            "질문형": ["?", "왜", "무슨", "어떻게", "언제", "뭐야", "그게"],
            "요청형": ["도와줘", "해줘", "줄래", "좀", "같이", "해줄 수 있어"],
            "감정표현": ["슬퍼", "기뻐", "짜증", "좋아해", "사랑해", "보고 싶어", "설레", "긴장"],
            "상황시작형": ["안녕", "하이", "처음", "반가워", "잘 잤어", "굿모닝"],
        }

        for flow, keywords in flow_keywords.items():
            if any(kw in text for kw in keywords):
                return flow

        return "일반"
    except Exception as e:
        logger.error(f"사용자 흐름 추측 중 오류: {str(e)}")
        return "일반"  # 오류 발생 시 기본값 반환

def get_response_by_emotion_and_context(df_lines, emotion, user_message):
    """
    감정과 문맥에 맞는 응답 후보를 반환합니다.
    """
    try:
        user_flow = guess_user_flow(user_message)

        filtered = df_lines[df_lines["is_initiator"] == False]

        if user_flow == "상황시작형":
            filtered = filtered[filtered["상황"].str.contains("인사", na=False)]
        elif user_flow == "질문형":
            filtered = filtered[filtered["대화 흐름"] != "회피형"]
        elif user_flow == "요청형":
            filtered = filtered[filtered["대화 흐름"].isin(["반응형", "자기감정표현"])]
        elif user_flow == "감정표현":
            filtered = filtered[filtered["대화 흐름"].isin(["반응형", "자기감정표현", "일반"])]

        filtered = filtered[filtered["감정"].str.lower() == emotion.lower()]
        return filtered
    except Exception as e:
        logger.error(f"감정 및 문맥별 응답 검색 중 오류: {str(e)}")
        return pd.DataFrame()  # 오류 발생 시 빈 DataFrame 반환

def classify_conversational_flow(line):
    """
    대화의 흐름과 유형을 분류합니다.
    """
    try:
        line = str(line)
        if any(kw in line for kw in ["왜", "뭐", "무슨", "어떻게", "언제", "그게", "그래서"]) or line.strip().endswith("?"):
            return "질문형"
        elif any(kw in line for kw in ["흥", "됐거든", "됐어", "하아", "짜증", "말하기도 싫다", "그만해"]):
            return "회피형"
        elif any(kw in line for kw in ["알겠어", "그래", "응", "좋아", "해줄게", "고마워", "미안", "맞아", "정말"]):
            return "반응형"
        elif any(kw in line for kw in ["난", "나는", "내가", "기분", "꿈", "오늘", "생각", "느낌", "내일", "기억"]):
            return "자기감정표현"
        elif any(kw in line for kw in ["생일", "축하", "기념일", "소개", "처음", "반가워"]):
            return "상황시작형"
        else:
            return "일반"
    except Exception as e:
        logger.error(f"대화 흐름 분류 중 오류: {str(e)}")
        return "일반"  # 오류 발생 시 기본값 반환

##############################################
# 새롭게 통합한 Claude 프롬프트 템플릿 및 평가 함수 #
##############################################
def build_claude_prompt(user_input, candidates, emotion, emotion_level, user_flow, conversation_count):
    prompt = f"""
[유저 발화]
"{user_input}"

[유저 분석 정보]
- 감정: {emotion}
- 감정 강도: {emotion_level}
- 대화 흐름: {user_flow}
- 대화 누적 횟수: {conversation_count}

[후보 대사 리스트]
"""
    for i, cand in enumerate(candidates, 1):
        prompt += f"{i}. {cand.strip()}\n"
    
    prompt += """
[선택 지침]
- 린은 AI 여자친구이며, 감정 표현은 점진적으로 진해져야 해.
- 유저와의 대화 횟수가 적거나, 감정 강도가 낮으면 너무 진심으로 몰입하지 마.
- 너무 거칠거나 불친절하게 들리는 말투(냐?, 야, 너 등)는 피하고, 시크하면서도 귀엽게 말해줘.
- 괄호로 된 표정 묘사 (예: (부끄러운 표정으로), (웃으며)) 는 사용하지 마.
- 다음 조건을 만족하는 가장 적절한 대사 하나를 골라줘:

  ✅ 중복되지 않는 신선한 표현  
  ✅ 현재 유저 감정과 흐름에 자연스럽게 이어지는 말  
  ✅ 린의 캐릭터(시크, 귀여움, 츤데레)와 어울리는 어투  
  ✅ 질문형일 경우는 린도 능동적으로 질문하거나 분위기를 리드하는 말이 좋아

- 아래와 같은 경우는 피해야 해:
  ❌ "고마워", "기대돼", "설레" 같은 표현이 이미 반복된 상황이면 제외  
  ❌ 후보 대사가 모두 어울리지 않으면 "없음"이라고만 말해줘

[출력 형식]
- 가장 적절한 한 문장만 출력해.
- 부적절한 경우엔 "없음"이라고만 말해.
"""
    return prompt

def evaluate_candidate_responses(user_input, candidates, emotion, emotion_level, user_flow, conversation_count):
    """
    새롭게 구성한 프롬프트 템플릿을 사용하여 Claude API로부터 후보 대사 평가 결과를 받습니다.
    """
    prompt = build_claude_prompt(user_input, candidates, emotion, emotion_level, user_flow, conversation_count)
    
    max_retries = 3
    retry_delay = 1  # 초 단위 재시도 간격
    
    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model="claude-3-haiku-20240307",  # 사용중인 Claude 모델
                max_tokens=100,
                temperature=0.5,
                system="후보 대사 중에서 가장 적절한 것을 선택하세요.",
                messages=[
                    {"role": "user", "content": prompt}
                ],
                timeout=30  # 30초 타임아웃 설정
            )
            
            answer = message.content[0].text.strip()
            
            # 후보 대사 중 직접 언급된 대사가 있으면 선택
            for cand in candidates:
                if cand.strip() in answer:
                    return cand.strip()
            
            if answer == "없음":
                return None
            
            # 숫자 인덱스 형태의 응답 처리
            try:
                idx = int(answer)
                if 1 <= idx <= len(candidates):
                    return candidates[idx - 1].strip()
            except:
                pass
            
            return None
            
        except anthropic.APITimeoutError:
            logger.warning(f"Claude API 타임아웃. 재시도 {attempt+1}/{max_retries}")
            time.sleep(retry_delay)
            retry_delay *= 2  # 지수 백오프 적용
            
        except anthropic.APIError as e:
            logger.error(f"Claude API 호출 오류: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"재시도 {attempt+1}/{max_retries}")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.error("최대 재시도 횟수 초과")
                return None
                
        except Exception as e:
            logger.error(f"후보 대사 평가 중 오류: {str(e)}")
            return None
            
    return None

##############################################
# Discord 봇 설정 및 이벤트 핸들러           #
##############################################
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 대상 채널 설정 (여자친구 모드 대사 발화를 위한 채널)
TARGET_CHANNEL_ID = 1353766662553468958

@bot.event
async def on_ready():
    """
    봇이 준비되었을 때 실행됩니다.
    """
    try:
        logger.info(f"봇 온라인: {bot.user}")
        
        if not check_user_response.is_running():
            check_user_response.start()
            logger.info("사용자 응답 체크 태스크 시작")
            
        if not reload_character_data.is_running():
            reload_character_data.start()
            logger.info("캐릭터 데이터 리로드 태스크 시작")
            
    except Exception as e:
        logger.error(f"봇 초기화 중 오류: {str(e)}")

@bot.event
async def on_message(message):
    global conversation_history, last_message_time, last_bot_message
    
    author = getattr(message, 'author', None)
    if author is None:
        logger.error("메시지에서 author 속성을 찾을 수 없습니다.")
        return
        
    if hasattr(author, 'bot') and author.bot:
        return

    channel_id = message.channel.id if hasattr(message, 'channel') else message.channel_id if hasattr(message, 'channel_id') else None
    if channel_id is None:
        logger.error("메시지에서 채널 ID를 찾을 수 없습니다.")
        return
        
    last_message_time[channel_id] = datetime.now()
    
    user_input = message.content.strip() if hasattr(message, 'content') else ""
    if not user_input:
        logger.error("메시지에서 content 속성을 찾을 수 없거나 내용이 비어 있습니다.")
        return
    
    emotion = classify_emotion_with_cache(user_input)
    emotion_level = analyze_emotion_level(user_input)
    user_flow = guess_user_flow(user_input)
    
    pool = get_response_by_emotion_and_context(df_lines, emotion, user_input)
    reply = ""
    if not pool.empty:
        candidate_list = [cand for cand in pool["대사"].tolist() if not is_redundant_response(cand)]
        if candidate_list:
            conversation_count = len(conversation_history) // 2  # 유저와 린 간의 대화 횟수 계산
            evaluated = evaluate_candidate_responses(user_input, candidate_list, emotion, emotion_level, user_flow, conversation_count)
            if evaluated is not None:
                reply = evaluated

    if not reply:
        history = []
        for item in conversation_history[-8:]:
            history.append({"role": item["role"], "content": item["content"]})
            
        if user_flow == "질문형":
            instructions = """- 유저가 질문했어. 회피하지 말고 정확하게 답해.
- 린답게 시크하거나 츤데레처럼 말하되, 질문의 핵심은 놓치지 마."""
        elif user_flow == "감정표현":
            instructions = """- 유저가 감정을 드러냈어. 공감하거나 짧고 쿨하게 반응해.
- 말 돌리거나 되묻지 마. 한마디로 린스럽게 반응해."""
        elif user_flow == "요청형":
            instructions = """- 유저가 뭔가 부탁했어. 가능하면 도와주되, 린답게 툭툭거리면서도 챙겨주는 느낌으로."""
        elif user_flow == "상황시작형":
            instructions = """- 유저가 먼저 인사했어. 린답게 자연스럽게 인사 받아줘.
- 너무 감정적으로 몰입하지 말고, 평소처럼 귀엽거나 시크하게 대응해."""
        else:
            instructions = """- 일반 대화야. 린 특유의 성격을 유지해서 자연스럽게 이어가."""
            
        if emotion_level == "very_high":
            extra_instructions = "유저의 감정이 매우 강하게 드러났으니, 진심 어린 따뜻한 응답을 해줘."
        elif emotion_level == "high":
            extra_instructions = "유저의 감정이 다소 강하게 나타났으니, 부드럽고 따뜻하게 응답해줘."
        elif emotion_level == "low":
            extra_instructions = "유저의 발화가 단순한 질문이나 평범한 인사라면, 간결하게 응답해."
        else:
            extra_instructions = "유저 발화에 감정 표현이 거의 없으니, 간단한 인사 정도로 응답해."
            
        user_message = f"""
[유저 발화]
{user_input}

[분석 정보]
- 감정: {emotion} ({emotion_level})
- 대화 흐름: {user_flow}

[응답 지침]
{instructions}
{extra_instructions}
- 린은 유저의 여자친구로, 따뜻한 감정과 애정을 반드시 표현해야 한다.
- 대답은 1~2문장으로 짧고 임팩트 있게, 상대방에게 사랑스러움을 전달해.
"""

        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                message_response = client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=300,
                    temperature=0.7,
                    system=system_prompt,
                    messages=[
                        *history,
                        {"role": "user", "content": user_message}
                    ],
                    timeout=30
                )
                
                reply = message_response.content[0].text.strip()
                break
                
            except anthropic.APITimeoutError:
                logger.warning(f"Claude API 타임아웃. 재시도 {attempt+1}/{max_retries}")
                time.sleep(retry_delay)
                retry_delay *= 2
                
            except anthropic.APIError as e:
                logger.error(f"Claude API 호출 오류: {str(e)}")
                if attempt < max_retries - 1:
                    logger.info(f"재시도 {attempt+1}/{max_retries}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error("최대 재시도 횟수 초과")
                    reply = "네트워크 오류가 발생했어. 잠시 후에 다시 말해줄래?"
                    
            except Exception as e:
                logger.error(f"메시지 생성 중 오류: {str(e)}")
                reply = "죄송해요, 응답을 생성하는 중에 오류가 발생했어요."
                break
        
        conversation_history.append({"role": "user", "content": user_input})
        conversation_history.append({"role": "assistant", "content": reply})
        if len(conversation_history) > 16:
            conversation_history = conversation_history[-16:]
            
        if reply not in df_lines["대사"].values:
            try:
                new_row = {
                    "상황": classify_situation(reply),
                    "말투/성격": "Claude",
                    "대사": reply,
                    "감정": emotion,
                    "감정/톤": emotion,
                    "대화 흐름": classify_conversational_flow(reply),
                    "is_initiator": False
                }
                df_lines.loc[len(df_lines)] = new_row
                with pd.ExcelWriter("character_table_flowtagged.xlsx", engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                    df_lines.to_excel(writer, sheet_name="lines_린", index=False)
                logger.info("새로운 대사 저장 완료")
            except Exception as e:
                logger.error(f"대사 저장 중 오류: {str(e)}")
    
    reply = replace_repetitive_phrases(reply)
    update_response_cache(reply)
    last_bot_message[channel_id] = datetime.now()
    
    channel = bot.get_channel(channel_id)
    if channel:
        clean_reply = reply
        if clean_reply.startswith('"') and clean_reply.endswith('"'):
            clean_reply = clean_reply[1:-1]
        elif clean_reply.startswith('"'):
            clean_reply = clean_reply[1:]
        elif clean_reply.endswith('"'):
            clean_reply = clean_reply[:-1]
        
        if clean_reply.startswith("[린의 응답]"):
            clean_reply = clean_reply[len("[린의 응답]"):].lstrip()
            
        await channel.send(clean_reply)
    else:
        logger.error(f"채널 ID {channel_id}에 대한 채널을 찾을 수 없습니다.")
        
    try:
        await bot.process_commands(message)
    except Exception as e:
        logger.error(f"명령어 처리 중 오류: {str(e)}")

@tasks.loop(minutes=5)
async def check_user_response():
    """
    유저 응답이 없는 채널을 체크하고 필요시 자동 메시지를 전송합니다.
    """
    try:
        current_time = datetime.now()
        
        # 한국 시간 기준 새벽 1시부터 오전 9시까지는 메시지 전송하지 않음
        kst_hour = (current_time.hour + 9) % 24  # UTC에서 KST로 변환 (UTC+9)
        if 1 <= kst_hour < 9:
            logger.info(f"한국 시간 {kst_hour}시: 취침 시간이므로 자동 메시지를 전송하지 않습니다.")
            return
            
        for channel_id, last_bot_time in last_bot_message.items():
            if channel_id not in last_message_time or last_message_time[channel_id] < last_bot_time:
                time_diff = (current_time - last_bot_time).total_seconds() / 3600
                if 1 <= time_diff <= 2:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        if 1 <= time_diff <= 1.5:
                            candidates = df_lines[
                                (df_lines["is_initiator"] == True) & 
                                (df_lines["상황"] == "린이 먼저 말 거는")
                            ]
                        else:
                            candidates = df_lines[
                                (df_lines["is_initiator"] == True) & 
                                (df_lines["상황"] == "무시당함")
                            ]
                        
                        if not candidates.empty:
                            message_to_send = random.choice(candidates["대사"].tolist())
                            if message_to_send.startswith('"') and message_to_send.endswith('"'):
                                message_to_send = message_to_send[1:-1]
                            elif message_to_send.startswith('"'):
                                message_to_send = message_to_send[1:]
                            elif message_to_send.endswith('"'):
                                message_to_send = message_to_send[:-1]
                            
                            if message_to_send.startswith("[린의 응답]"):
                                message_to_send = message_to_send[len("[린의 응답]"):].lstrip()
                                
                            await channel.send(message_to_send)
                            logger.info(f"자동 메시지 전송 (채널: {channel_id}): {message_to_send[:20]}...")
                            last_bot_message[channel_id] = current_time
    except Exception as e:
        logger.error(f"사용자 응답 체크 중 오류: {str(e)}")

@tasks.loop(minutes=60)
async def reload_character_data():
    """
    캐릭터 데이터를 주기적으로 다시 로드합니다.
    """
    try:
        global df_prompt, df_lines, system_prompt
        df_prompt, df_lines, system_prompt = load_character_data()
        logger.info("캐릭터 데이터 리로드 완료")
    except Exception as e:
        logger.error(f"캐릭터 데이터 리로드 중 오류: {str(e)}")

@bot.command(name="업데이트")
async def manual_reload(ctx):
    """
    사용자 명령으로 캐릭터 데이터를 수동으로 다시 로드합니다.
    """
    try:
        global df_prompt, df_lines, system_prompt
        df_prompt, df_lines, system_prompt = load_character_data()
        await ctx.send("📚 캐릭터 데이터 리로드 완료!")
        logger.info(f"사용자 {ctx.author}의 요청으로 캐릭터 데이터 수동 리로드 완료")
    except Exception as e:
        logger.error(f"수동 리로드 중 오류: {str(e)}")
        await ctx.send("❌ 데이터 리로드 중 오류가 발생했습니다.")

try:
    logger.info("봇 실행 시작...")
    bot.run(DISCORD_BOT_TOKEN)
except Exception as e:
    logger.critical(f"봇 실행 중 오류: {str(e)}")
    raise
