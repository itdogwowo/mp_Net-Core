from machine import Timer, I2C, ADC, Pin, PWM,UART
import esp, gc, time, json,  neopixel, utime, struct  ,ubinascii 
from lib.LEDController import *
from lib.ConfigManager import *
from lib.pca9685 import *    
from lib.apa102 import *
from lib.dispatch import dprint

def init_i2c_led(i2c_led):
    re_ledPwm = []

    led_IO = {'led_IO':i2c_led,'Q':16}
    ledPwm = LEDcontroller('i2c_LED',led_IO)

    return re_ledPwm

def init_UART(uart_io):
    uart = None
    if uart_io['enable'] :
        uart = UART(1, baudrate=uart_io['baudrate'], bits=8, tx=uart_io['GPIO']['tx'], rx=uart_io['GPIO']['rx'])
    return uart

def init_i2c(led_io):
    i2c_led_list = []
    if led_io['enable'] :
        for i2cc in led_io['i2c_List']:
            dprint(f"I2C Init: SCL={i2cc['GPIO']['scl']}, SDA={i2cc['GPIO']['sda']}")
            i2c = I2C(scl=i2cc['GPIO']['scl'], sda=i2cc['GPIO']['sda'])
            #dprint(i2c.scan())
            for i in i2c.scan():
                dprint(f"Found I2C Device: {hex(i)}")
            for i in i2cc['address']:
                try:
                    pca = PCA9685(i2c,address=int(i,16))
                    pca.freq(1000)
                    # i2c_Object = init_i2c_led(pca)
                    # i2c_led_list.append(i2c_Object)

                    led_IO = {'led_IO':pca,'Q':16}
                    ledPwm = LEDcontroller('i2c_LED',led_IO)
                    i2c_led_list.append(ledPwm)

                except BaseException as e:
                    dprint(f'missing address : {i}')
                    led_IO = {'led_IO':None,'Q':16}
                    ledPwm = LEDcontroller('v_i2c_LED',led_IO)
                    i2c_led_list.append(ledPwm)
                    
    return i2c_led_list

def init_led(led_io):
    led_l = []
    if led_io['enable'] :
        led_IO = {'led_IO':led_io['GPIO_List'],'Q':len(led_io['GPIO_List']),'i2c_Object':''}
        led_l.append(LEDcontroller('esp_LED',led_IO))
    return led_l

def init_rgb(led_io):
    rgb_l = []
    if led_io['enable'] :
        for i in led_io['GPIO'] :
            led_IO = {'led_IO':i['GPIO'],'Q':i['Q'],'i2c_Object':''}
            rgb = LEDcontroller('RGB',led_IO)
            rgb_l.append(rgb)
#     debugPrint(rgb_l)
    return rgb_l

def init_i2s(led_io):
    
    i2s = None
    if led_io['enable'] :
        from lib.audio_tools import AudioBuffer
        from machine import I2S

        # 硬件引脚配置 (ESP32)
        sck_pin = Pin(led_io['i2s_List'][0]['GPIO']['sck'])   # 串行时钟
        ws_pin = Pin(led_io['i2s_List'][0]['GPIO']['ws'])    # 字选择（声道时钟）
        sd_pin = Pin(led_io['i2s_List'][0]['GPIO']['sd'])    # 串行数据输入

        # I2S参数配置
        sample_rate = led_io['sampling_rate']  # 采样率 (Hz)
        sample_bits = led_io['sample_bits']     # 采样位深
        buffer_frames = led_io['buffer_frames']  # 样本帧数（每个帧包含左右声道）
        channel_to_use = led_io['channel_to_use']   # 0=左声道, 1=右声道（根据实际硬件连接选择）

        # 初始化I2S（立体声模式）
        _i2s = I2S(
            0,                              # 使用I2S0
            sck=sck_pin,                    # 时钟引脚
            ws=ws_pin,                      # 字选择引脚
            sd=sd_pin,                      # 数据引脚
            mode=I2S.RX,                    # 接收模式
            bits=sample_bits,               # 采样位数
            format=I2S.STEREO,              # 立体声
            rate=sample_rate,               # 采样率
            ibuf=buffer_frames * 4 * 2,       # 输入缓冲区大小
        )
        


        i2s = AudioBuffer(
            i2s_device=_i2s,
            buffer_size=buffer_frames,
            history_size=15,
            beat_threshold=1.5,
            debug=0
        )


    return i2s
