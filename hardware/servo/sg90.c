#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>

#define PWM_PATH "/sys/class/pwm/pwmchip0/pwm0"

void write_pwm(const char *file, const char *value)
{
    char path[256];

    snprintf(path, sizeof(path), "%s/%s", PWM_PATH, file);

    int fd = open(path, O_WRONLY);

    if (fd < 0)
    {
        perror("open");
        exit(1);
    }

    if (write(fd, value, strlen(value)) < 0)
    {
        perror("write");
        close(fd);
        exit(1);
    }

    close(fd);
}

int main(int argc, char *argv[])
{
    const char *pulse;

    if (argc != 2)
    {
        fprintf(stderr, "Usage: %s open|close\n", argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "open") == 0)
    {
        pulse = "1000000";
    }
    else if (strcmp(argv[1], "close") == 0)
    {
        pulse = "2000000";
    }
    else
    {
        fprintf(stderr, "Unknown command: %s\n", argv[1]);
        return 1;
    }

    // 처음 사용할 때만 PWM 주기를 설정
    if (access(PWM_PATH "/period", F_OK) != 0)
    {
        int fd = open("/sys/class/pwm/pwmchip0/export", O_WRONLY);

        if (fd < 0)
        {
            perror("open export");
            return 1;
        }

        if (write(fd, "0", 1) != 1)
        {
            perror("write export");
            close(fd);
            return 1;
        }

        close(fd);

        // 채널 파일이 준비될 때까지 최대 1초 대기
        for (int i = 0; i < 100; i++)
        {
            if (access(PWM_PATH "/period", F_OK) == 0)
                break;

            usleep(10000);
        }

        if (access(PWM_PATH "/period", F_OK) != 0)
        {
            fprintf(stderr, "PWM channel not ready\n");
            return 1;
        }
    }

    // 현재 주기 확인
    FILE *fp = fopen(PWM_PATH "/period", "r");
    unsigned long period;

    if (fp == NULL)
    {
        perror("fopen");
        return 1;
    }

    if (fscanf(fp, "%lu", &period) != 1)
    {
        fprintf(stderr, "Failed to read PWM period\n");
        fclose(fp);
        return 1;
    }

    fclose(fp);

    if (period != 20000000UL)
    {
        write_pwm("enable", "0");
        write_pwm("duty_cycle", "0");
        write_pwm("period", "20000000");
    }

    write_pwm("duty_cycle", pulse);
    write_pwm("enable", "1");

    printf("SG90 -> %s (PWM command sent)\n", argv[1]);

    // 위치 유지를 위해 PWM을 끄지 않음
    return 0;
}