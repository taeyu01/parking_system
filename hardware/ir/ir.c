#include <stdio.h>
#include <unistd.h>
#include <gpiod.h>

#define CHIP_NAME "gpiochip0"
#define IR_PIN 17

int main(void)
{
    struct gpiod_chip *chip;
    struct gpiod_line *line;
    int value;

    // GPIO chip 열기
    chip = gpiod_chip_open_by_name(CHIP_NAME);
    if (!chip)
    {
        perror("gpiod_chip_open_by_name");
        return 1;
    }

    // GPIO17 가져오기
    line = gpiod_chip_get_line(chip, IR_PIN);
    if (!line)
    {
        perror("gpiod_chip_get_line");
        gpiod_chip_close(chip);
        return 1;
    }

    // GPIO17을 입력으로 설정
    if (gpiod_line_request_input(line, "ir_sensor") < 0)
    {
        perror("gpiod_line_request_input");
        gpiod_chip_close(chip);
        return 1;
    }

    printf("Waiting for vehicle...\n");

    // 1단계: 차량이 센서에 들어올 때까지 기다림
    while (1)
    {
        value = gpiod_line_get_value(line);

        if (value < 0)
        {
            perror("gpiod_line_get_value");
            gpiod_line_release(line);
            gpiod_chip_close(chip);
            return 1;
        }

        if (value == 0)
        {
            printf("Vehicle detected\n");
            break;
        }

        usleep(50000); // 50ms
    }

    // 2단계: 차량이 센서를 완전히 지나갈 때까지 기다림
    while (1)
    {
        value = gpiod_line_get_value(line);

        if (value < 0)
        {
            perror("gpiod_line_get_value");
            gpiod_line_release(line);
            gpiod_chip_close(chip);
            return 1;
        }

        if (value == 1)
        {
            printf("Vehicle passed\n");
            break;
        }

        usleep(50000);
    }

    gpiod_line_release(line);
    gpiod_chip_close(chip);

    return 0;
}