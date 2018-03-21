# Copyright (C) 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#Botmation Terms of use
#This code is for personal entertainment use only.
#This code is not to be used commercially.

"""Sample that implements a gRPC client for the Google Assistant API."""

import concurrent.futures
import json
import logging
import os
import os.path
import sys
import uuid

import click
import grpc
import google.auth.transport.grpc
import google.auth.transport.requests
import google.oauth2.credentials

import speech_recognition as sr
import time
import RPi.GPIO as GPIO
import threading

from google.assistant.embedded.v1alpha2 import (
    embedded_assistant_pb2,
    embedded_assistant_pb2_grpc
)
from tenacity import retry, stop_after_attempt, retry_if_exception

try:
    from . import (
        assistant_helpers,
        audio_helpers,
        device_helpers
    )
except (SystemError, ImportError):
    import assistant_helpers
    import audio_helpers
    import device_helpers



ASSISTANT_API_ENDPOINT = 'embeddedassistant.googleapis.com'
END_OF_UTTERANCE = embedded_assistant_pb2.AssistResponse.END_OF_UTTERANCE
DIALOG_FOLLOW_ON = embedded_assistant_pb2.DialogStateOut.DIALOG_FOLLOW_ON
CLOSE_MICROPHONE = embedded_assistant_pb2.DialogStateOut.CLOSE_MICROPHONE
DEFAULT_GRPC_DEADLINE = 60 * 3 + 5


class SampleAssistant(object):
    """Sample Assistant that supports conversations and device actions.
    Args:
      device_model_id: identifier of the device model.
      device_id: identifier of the registered device instance.
      conversation_stream(ConversationStream): audio stream
        for recording query and playing back assistant answer.
      channel: authorized gRPC channel for connection to the
        Google Assistant API.
      deadline_sec: gRPC deadline in seconds for Google Assistant API call.
      device_handler: callback for device actions.
    """

    def __init__(self, language_code, device_model_id, device_id,
                 conversation_stream,
                 channel, deadline_sec, device_handler):
        self.language_code = language_code
        self.device_model_id = device_model_id
        self.device_id = device_id
        self.conversation_stream = conversation_stream

        # Opaque blob provided in AssistResponse that,
        # when provided in a follow-up AssistRequest,
        # gives the Assistant a context marker within the current state
        # of the multi-Assist()-RPC "conversation".
        # This value, along with MicrophoneMode, supports a more natural
        # "conversation" with the Assistant.
        self.conversation_state = None

        # Create Google Assistant API gRPC client.
        self.assistant = embedded_assistant_pb2_grpc.EmbeddedAssistantStub(
            channel
        )
        self.deadline = deadline_sec

        self.device_handler = device_handler

    def __enter__(self):
        return self

    def __exit__(self, etype, e, traceback):
        if e:
            return False
        self.conversation_stream.close()

    def is_grpc_error_unavailable(e):
        is_grpc_error = isinstance(e, grpc.RpcError)
        if is_grpc_error and (e.code() == grpc.StatusCode.UNAVAILABLE):
            logging.error('grpc unavailable error: %s', e)
            return True
        return False

    @retry(reraise=True, stop=stop_after_attempt(3),
           retry=retry_if_exception(is_grpc_error_unavailable))
    def assist(self):
        """Send a voice request to the Assistant and playback the response.
        Returns: True if conversation should continue.
        """
        continue_conversation = False
        device_actions_futures = []
        global dimstart
        global pin_num
        
        dimstart = 1 #Start dimming LED to signal ready status
        self.conversation_stream.start_recording()
        logging.info('Recording audio request.')
        t1 = threading.Thread(target=leddim,args=(5,)) #Dim second pin
        t1.start()

        def iter_assist_requests():
            for c in self.gen_assist_requests():
                assistant_helpers.log_assist_request_without_audio(c)
                yield c
            self.conversation_stream.start_playback()

        # This generator yields AssistResponse proto messages
        # received from the gRPC Google Assistant API.
        for resp in self.assistant.Assist(iter_assist_requests(),
                                          self.deadline):
            assistant_helpers.log_assist_response_without_audio(resp)
            if resp.event_type == END_OF_UTTERANCE:
                logging.info('End of audio request detected')
                self.conversation_stream.stop_recording()
                dimstart = 0 #Stop dimming
		#speech text
            if resp.speech_results:
                logging.info('Transcript of user request: "%s".',
                             ' '.join(r.transcript
                                      for r in resp.speech_results))
                logging.info('Playing assistant response.')
		#Possible text from google
                print(resp.dialog_state_out.supplemental_display_text)
            if len(resp.audio_out.audio_data) > 0:
                self.conversation_stream.write(resp.audio_out.audio_data)
            if resp.dialog_state_out.conversation_state:
                conversation_state = resp.dialog_state_out.conversation_state
                logging.debug('Updating conversation state.')
                self.conversation_state = conversation_state
            if resp.dialog_state_out.volume_percentage != 0:
                volume_percentage = resp.dialog_state_out.volume_percentage
                logging.info('Setting volume to %s%%', volume_percentage)
                self.conversation_stream.volume_percentage = volume_percentage
            if resp.dialog_state_out.microphone_mode == DIALOG_FOLLOW_ON:
                continue_conversation = True
                logging.info('Expecting follow-on query from user.')
            elif resp.dialog_state_out.microphone_mode == CLOSE_MICROPHONE:
                continue_conversation = False
                print('stop conversation')
            if resp.device_action.device_request_json:
                device_request = json.loads(
                    resp.device_action.device_request_json
                )
                fs = self.device_handler(device_request)
                if fs:
                    device_actions_futures.extend(fs)

        if len(device_actions_futures):
            logging.info('Waiting for device executions to complete.')
            concurrent.futures.wait(device_actions_futures)

        logging.info('Finished playing assistant response.')
        
        self.conversation_stream.stop_playback()
        print('stopped playback')
        #Need to close stream in order to release audio back to speech recog program
        if continue_conversation == False: self.conversation_stream.close()
        return continue_conversation

    def gen_assist_requests(self):
        """Yields: AssistRequest messages to send to the API."""
        #language_code=self.language_code
        dialog_state_in = embedded_assistant_pb2.DialogStateIn(
                language_code=new_lang,
                conversation_state=b''
            )
        if self.conversation_state:
            logging.debug('Sending conversation state.')
            dialog_state_in.conversation_state = self.conversation_state
        config = embedded_assistant_pb2.AssistConfig(
            audio_in_config=embedded_assistant_pb2.AudioInConfig(
                encoding='LINEAR16',
                sample_rate_hertz=self.conversation_stream.sample_rate,
            ),
            audio_out_config=embedded_assistant_pb2.AudioOutConfig(
                encoding='LINEAR16',
                sample_rate_hertz=self.conversation_stream.sample_rate,
                volume_percentage=self.conversation_stream.volume_percentage,
            ),
            dialog_state_in=dialog_state_in,
            device_config=embedded_assistant_pb2.DeviceConfig(
                device_id=self.device_id,
                device_model_id=self.device_model_id,
            )
        )
        # The first AssistRequest must contain the AssistConfig
        # and no audio data.
        yield embedded_assistant_pb2.AssistRequest(config=config)
        for data in self.conversation_stream:
            # Subsequent requests need audio data, but not config.
            yield embedded_assistant_pb2.AssistRequest(audio_in=data)



class SampleTextAssistant(object):
    """Sample Assistant that supports text based conversations.

    Args:
      language_code: language for the conversation.
      device_model_id: identifier of the device model.
      device_id: identifier of the registered device instance.
      channel: authorized gRPC channel for connection to the
        Google Assistant API.
      deadline_sec: gRPC deadline in seconds for Google Assistant API call.
    """

    def __init__(self, language_code, device_model_id, device_id,
                 conversation_stream,
                 channel, deadline_sec, device_handler):
        self.language_code = language_code
        self.device_model_id = device_model_id
        self.device_id = device_id
        self.conversation_stream = conversation_stream

 
        self.conversation_state = None

        # Create Google Assistant API gRPC client.
        self.assistant = embedded_assistant_pb2_grpc.EmbeddedAssistantStub(
            channel
        )
        self.deadline = deadline_sec

        self.device_handler = device_handler
        
    def __enter__(self):
        return self

    def __exit__(self, etype, e, traceback):
        if e:
            return False

    def assist(self, text_query):
        """Send a text request to the Assistant and playback the response.
        """
        #Need to start a new conversation stream to allow playback of assistant
        self.conversation_stream.start_recording()
        self.conversation_stream.stop_recording()
        #Set default language to English for the text to speech
        def iter_assist_requests():
            dialog_state_in = embedded_assistant_pb2.DialogStateIn(
                language_code='en-US',
                conversation_state=b''
            )
            if self.conversation_state:
                dialog_state_in.conversation_state = self.conversation_state
            config = embedded_assistant_pb2.AssistConfig(
                audio_out_config=embedded_assistant_pb2.AudioOutConfig(
                    encoding='LINEAR16',
                    sample_rate_hertz=16000,
                    volume_percentage=0,
                ),
                dialog_state_in=dialog_state_in,
                device_config=embedded_assistant_pb2.DeviceConfig(
                    device_id=self.device_id,
                    device_model_id=self.device_model_id,
                ),
                text_query=text_query,
            )
            req = embedded_assistant_pb2.AssistRequest(config=config)
            assistant_helpers.log_assist_request_without_audio(req)
            yield req
            self.conversation_stream.start_playback()

        display_text = None
        for resp in self.assistant.Assist(iter_assist_requests(),
                                          self.deadline):
            assistant_helpers.log_assist_response_without_audio(resp)
            if resp.dialog_state_out.conversation_state:
                conversation_state = resp.dialog_state_out.conversation_state
                self.conversation_state = conversation_state
            if resp.dialog_state_out.supplemental_display_text:
                display_text = resp.dialog_state_out.supplemental_display_text
            if len(resp.audio_out.audio_data) > 0:
                self.conversation_stream.write(resp.audio_out.audio_data)

        
            if resp.dialog_state_out.volume_percentage != 0:
                volume_percentage = resp.dialog_state_out.volume_percentage
                logging.info('Setting volume to %s%%', volume_percentage)
                self.conversation_stream.volume_percentage = volume_percentage

        logging.info('Finished playing assistant response.')
        self.conversation_stream.stop_playback()
        self.conversation_stream.close()
        return display_text


@click.command()
@click.option('--api-endpoint', default=ASSISTANT_API_ENDPOINT,
              metavar='<api endpoint>', show_default=True,
              help='Address of Google Assistant API service.')
@click.option('--credentials',
              metavar='<credentials>', show_default=True,
              default=os.path.join(click.get_app_dir('google-oauthlib-tool'),
                                   'credentials.json'),
              help='Path to read OAuth2 credentials.')
@click.option('--project-id',
              metavar='<project id>',
              help=('Google Developer Project ID used for registration '
                    'if --device-id is not specified'))
@click.option('--device-model-id',
              metavar='<device model id>',
              help=(('Unique device model identifier, '
                     'if not specifed, it is read from --device-config')))
@click.option('--device-id',
              metavar='<device id>',
              help=(('Unique registered device instance identifier, '
                     'if not specified, it is read from --device-config, '
                     'if no device_config found: a new device is registered '
                     'using a unique id and a new device config is saved')))
@click.option('--device-config', show_default=True,
              metavar='<device config>',
              default=os.path.join(
                  click.get_app_dir('googlesamples-assistant'),
                  'device_config.json'),
              help='Path to save and restore the device configuration')
@click.option('--lang', show_default=True,
              metavar='<language code>',
              default='en-US',
              help='Language code of the Assistant')
@click.option('--verbose', '-v', is_flag=True, default=False,
              help='Verbose logging.')
@click.option('--input-audio-file', '-i',
              metavar='<input file>',
              help='Path to input audio file. '
              'If missing, uses audio capture')
@click.option('--output-audio-file', '-o',
              metavar='<output file>',
              help='Path to output audio file. '
              'If missing, uses audio playback')
@click.option('--audio-sample-rate',
              default=audio_helpers.DEFAULT_AUDIO_SAMPLE_RATE,
              metavar='<audio sample rate>', show_default=True,
              help='Audio sample rate in hertz.')
@click.option('--audio-sample-width',
              default=audio_helpers.DEFAULT_AUDIO_SAMPLE_WIDTH,
              metavar='<audio sample width>', show_default=True,
              help='Audio sample width in bytes.')
@click.option('--audio-iter-size',
              default=audio_helpers.DEFAULT_AUDIO_ITER_SIZE,
              metavar='<audio iter size>', show_default=True,
              help='Size of each read during audio stream iteration in bytes.')
@click.option('--audio-block-size',
              default=audio_helpers.DEFAULT_AUDIO_DEVICE_BLOCK_SIZE,
              metavar='<audio block size>', show_default=True,
              help=('Block size in bytes for each audio device '
                    'read and write operation.'))
@click.option('--audio-flush-size',
              default=audio_helpers.DEFAULT_AUDIO_DEVICE_FLUSH_SIZE,
              metavar='<audio flush size>', show_default=True,
              help=('Size of silence data in bytes written '
                    'during flush operation'))
@click.option('--grpc-deadline', default=DEFAULT_GRPC_DEADLINE,
              metavar='<grpc deadline>', show_default=True,
              help='gRPC deadline in seconds')
@click.option('--once', default=False, is_flag=True,
              help='Force termination after a single conversation.')


def main(api_endpoint, credentials, project_id,
         device_model_id, device_id, device_config, lang, verbose,
         input_audio_file, output_audio_file,
         audio_sample_rate, audio_sample_width,
         audio_iter_size, audio_block_size, audio_flush_size,
         grpc_deadline, once, *args, **kwargs):
    """Samples for the Google Assistant API.
    Examples:
      Run the sample with microphone input and speaker output:
        $ python -m googlesamples.assistant
      Run the sample with file input and speaker output:
        $ python -m googlesamples.assistant -i <input file>
      Run the sample with file input and output:
        $ python -m googlesamples.assistant -i <input file> -o <output file>
    """
    global run_once
    global switchover
    
    
    if run_once:
        print("running main code")
        run_once = 1
        # Setup logging.
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)

        # Load OAuth 2.0 credentials.
        try:
            with open(credentials, 'r') as f:
                credentials = google.oauth2.credentials.Credentials(token=None,
                                                                **json.load(f))
                http_request = google.auth.transport.requests.Request()
                credentials.refresh(http_request)
        except Exception as e:
            logging.error('Error loading credentials: %s', e)
            logging.error('Run google-oauthlib-tool to initialize '
                          'new OAuth 2.0 credentials.')
            sys.exit(-1)

        # Create an authorized gRPC channel.
        grpc_channel = google.auth.transport.grpc.secure_authorized_channel(
            credentials, http_request, api_endpoint)
        logging.info('Connecting to %s', api_endpoint)
    
    # Configure audio source and sink.
    audio_device = None
    if input_audio_file:
        audio_source = audio_helpers.WaveSource(
            open(input_audio_file, 'rb'),
            sample_rate=audio_sample_rate,
            sample_width=audio_sample_width
        )
    else:
        audio_source = audio_device = (
            audio_device or audio_helpers.SoundDeviceStream(
                sample_rate=audio_sample_rate,
                sample_width=audio_sample_width,
                block_size=audio_block_size,
                flush_size=audio_flush_size
            )
        )
    if output_audio_file:
        audio_sink = audio_helpers.WaveSink(
            open(output_audio_file, 'wb'),
            sample_rate=audio_sample_rate,
            sample_width=audio_sample_width
        )
    else:
        audio_sink = audio_device = (
            audio_device or audio_helpers.SoundDeviceStream(
                sample_rate=audio_sample_rate,
                sample_width=audio_sample_width,
                block_size=audio_block_size,
                flush_size=audio_flush_size
            )
        )

    # Create conversation stream with the given audio source and sink.
    conversation_stream = audio_helpers.ConversationStream(
        source=audio_source,
        sink=audio_sink,
        iter_size=audio_iter_size,
        sample_width=audio_sample_width,
    )

    device_handler = device_helpers.DeviceRequestHandler(device_id)

    @device_handler.command('action.devices.commands.OnOff')
    def onoff(on):
        if on:
            logging.info('Turning device on')
        else:
            logging.info('Turning device off')

    if not device_id or not device_model_id:
        try:
            with open(device_config) as f:
                device = json.load(f)
                device_id = device['id']
                device_model_id = device['model_id']
        except Exception as e:
            logging.warning('Device config not found: %s' % e)
            logging.info('Registering device')
            if not device_model_id:
                logging.error('Option --device-model-id required '
                              'when registering a device instance.')
                sys.exit(-1)
            if not project_id:
                logging.error('Option --project-id required '
                              'when registering a device instance.')
                sys.exit(-1)
            device_base_url = (
                'https://%s/v1alpha2/projects/%s/devices' % (api_endpoint,
                                                             project_id)
            )
            device_id = str(uuid.uuid1())
            payload = {
                'id': device_id,
                'model_id': device_model_id,
                'client_type': 'SDK_SERVICE'
            }
            session = google.auth.transport.requests.AuthorizedSession(
                credentials
            )
            r = session.post(device_base_url, data=json.dumps(payload))
            if r.status_code != 200:
                logging.error('Failed to register device: %s', r.text)
                sys.exit(-1)
            logging.info('Device registered: %s', device_id)
            os.makedirs(os.path.dirname(device_config), exist_ok=True)
            with open(device_config, 'w') as f:
                json.dump(payload, f)

#Text input
    print("Playing custom text audio")
    #During initial hotword we will need to custom play audio through text assistant.
    if switchover:
        print("part 1")
        #Set switchover to 0 so startup audio google assistant.
        switchover = 0
        with SampleTextAssistant(lang, device_model_id, device_id,conversation_stream,
                             grpc_channel, grpc_deadline,device_handler) as textassistant:
            #while True:
            #text_query = click.prompt('')
            #click.echo('<you> %s' % text_query)
            text_query = utext_query
            display_text = textassistant.assist(text_query=text_query)
            click.echo('<@assistant> %s' % display_text)
            main2()
                    
    else:    
        
        switchover = 1
        print("part 2")
        with SampleAssistant(lang, device_model_id, device_id,
                             conversation_stream,
                             grpc_channel, grpc_deadline,
                             device_handler) as assistant:
        # If file arguments are supplied:
        # exit after the first turn of the conversation.
            if input_audio_file or output_audio_file:
                assistant.assist()
                return

        # If no file arguments supplied:
        # keep recording voice requests using the microphone
        # and playing back assistant response using the speaker.
        # When the once flag is set, don't wait for a trigger. Otherwise, wait.
            wait_for_user_trigger = not once
        #wait_for_user_trigger = once
            while True:
                #if wait_for_user_trigger:
                #    click.pause(info='Press Enter to send a new request...')
                continue_conversation = assistant.assist()
                # wait for user trigger if there is no follow-up turn in
                # the conversation.
                wait_for_user_trigger = not continue_conversation
                if wait_for_user_trigger:
                    
                    speech()
                # If we only want one conversation, break.
                if once and (not continue_conversation):
                    
                    break


def main2():
    main()

#sample_rate = 48000 #May need to increase sample rate if recognition is having issues
def speech():

        global utext_query
        global new_lang
        global switchover
        global dimstart
        global pin_num
        r = sr.Recognizer()
        chan_list = [3,5,7,8,10,11,12,13,15,16,18,19,21,22,23,24]
        GPIO.setup(chan_list, GPIO.OUT)
        
        with sr.Microphone(sample_rate = 48000) as source:
            print("Say something!")
            audio = r.listen(source)

            myphrase = "blank"
            # recognize speech using Google Speech Recognition
            try:# for testing purposes, we're just using the default API key
		# to use another API key, use `r.recognize_google(audio, key="GOOGLE_SPEECH_RECOGNITION_API_KEY")`
		# instead of `r.recognize_google(audio)`
                print("Processing")
                myphrase = r.recognize_google(audio)
                myquery = myphrase
                print("Google Speech Recognition thinks you said " + r.recognize_google(audio))
            except sr.UnknownValueError:
                print("Google Speech Recognition could not understand audio")
                return
            except sr.RequestError as e:
                print("Could not request results from Google Speech Recognition service; {0}".format(e))

        GPIO.output(chan_list, 0)
        if 'German' in myphrase:
            print('Sending text to Assistant')
            print('Switching to German')
            
            utext_query = "Say yes bot imation in german"
            switchover = 1
            new_lang = 'de-DE'
            GPIO.output(3, 1) #Pins 3 and 5
            pin_num = 5 #Dim second pin number assignment
            main()
        if 'Spanish' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Spanish')
            utext_query = "Say hello bot imation in spanish"
            switchover = 1
            new_lang = 'es-ES'
            GPIO.output(7, 1) #Pins 7,8
            main()
        if 'Spanish neutral' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Spanish neutral')
            utext_query = "Say hello bot imation in spanish"
            switchover = 1
            new_lang = 'es-419'
            GPIO.output(7, 1)
            main()
        if 'French Canada' in myphrase:
            print('Sending text to Assistant')
            print('Switching to French canada')
            utext_query = "Say yes bot imation in french"
            switchover = 1
            new_lang = 'fr-CA'
            main()
        if 'French' in myphrase:
            print('Sending text to Assistant')
            print('Switching to French')
            utext_query = "Say yes bot imation in french"
            switchover = 1
            new_lang = 'fr-FR'
            GPIO.output(10, 1)#Pins 10,12
            main()

        if 'Japanese' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Japanese')
            utext_query = "Say good morning bot mation in japanese"
            switchover = 1
            new_lang = 'ja-JP'
            GPIO.output(11, 1)#Pins 11,13
            main()
        if 'Korean' in myphrase:
            print('Sending text to Assistant')
            print('Switching to korean')
            utext_query = "Say good evening bot mation in korean"
            switchover = 1
            new_lang = 'ko-KR'
            GPIO.output(15, 1)#Pins 15, 16
            main()
        if 'Italian' in myphrase:
            print('Sending text to Assistant')
            print('Switching to italian')
            utext_query = "Say how can I help you in italian"
            switchover = 1
            new_lang = 'it-IT'
            GPIO.output(18, 1) #Pins 18, 19
            main()
        if 'English' in myphrase:
            print('Sending text to Assistant')
            print('Switching to English')
            utext_query = "repeat after me Botmation ready"
            switchover = 1
            new_lang = 'en-US'
            GPIO.output(21, 1) #Pins 21, 22
            main()  
        if 'Australian' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Australian')
            utext_query = "how do you say Botmation ready in en-AU"
            switchover = 1
            new_lang = 'en-AU'
            main()		
        if 'British' in myphrase:
            print('Sending text to Assistant')
            print('Switching to British')
            utext_query = "How do you say Botmation ready in Britan"
            switchover = 1
            new_lang = 'en-GB'
            main()
        if 'Canadian' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Canadian')
            utext_query = "how do you say Botmation ready in Canada"
            switchover = 1
            new_lang = 'en-CA'
            main()
        if 'Portuguese' in myphrase:
            print('Sending text to Assistant')
            print('Switching to Portuguese')
            utext_query = "Say hello in Portuguese"
            switchover = 1
            new_lang = 'pt-BR'
            GPIO.output(23, 1) #Pins 23, 24
            main()		
        GPIO.cleanup()
        dimstart = 0

#thread.start_new_thread(someFunc, ())
#This will dim the associated LED to signal when to speak to google.
def leddim(pin):


	GPIO.setup(pin, GPIO.OUT)
	global dimstart 

	p = GPIO.PWM(pin, 50)  # channel=12 frequency=50Hz
	p.start(0) #Enter 0-100 for brightness level start value

	while dimstart:
		for dc in range(0, 101, 5): #range([start], stop[, step])
			p.ChangeDutyCycle(dc)
			time.sleep(0.1)
			print('Dim level' + str(dc))
		for dc in range(100, -1, -5):
			p.ChangeDutyCycle(dc)
			time.sleep(0.1)

	p.stop()
	
#Initial startup variable
run_once = 1
new_lang = 'en-US'
skip = 1 #1 to run speech
sr.energy_threshold = 400 #Higher value better for loud areas
utext_query = 'default' #used for sending queries to google
switchover = 1 #used to initiate hello in new language
dimstart = False #used to control dimming of LEDs
GPIO.setmode(GPIO.BOARD)

#if __name__ == '__main__':
while True:
    print('starting point')
    speech()
    main()

