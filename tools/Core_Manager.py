from machine import UART, Pin
import time

# 初始化 UART1 (通道 1, 鮑率 9600, TX 接 Pin 8, RX 接 Pin 9)
uart = UART(2, baudrate=115200, tx=Pin(12), rx=Pin(13))


cmd = bytearray(20)

cmd[0] = 0xFF

cmd[1] = 0x02

cmd[2] = 0xF0  
cmd[3] = 0xF0
cmd[4] = 0xF0

cmd[5] = 0x00 


while True:
    
    if uart.any():
        raw_data = uart.read()
        int_list = list(raw_data)
#         print(int_list)
        print(" ".join([f"{b:02X}" for b in raw_data]))
        
    time.sleep_ms(500)
