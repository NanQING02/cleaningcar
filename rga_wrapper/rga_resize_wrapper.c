#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#include <rga/im2d.h>

int rga_resize_bgr(
    const uint8_t* src, int sw, int sh,
    uint8_t* dst, int dw, int dh
) {
    if (!src || !dst || sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) {
        return -1;
    }

    rga_buffer_t src_buf = wrapbuffer_virtualaddr(
        (void*)src,
        sw,
        sh,
        RK_FORMAT_BGR_888
    );
    rga_buffer_t dst_buf = wrapbuffer_virtualaddr(
        (void*)dst,
        dw,
        dh,
        RK_FORMAT_BGR_888
    );

    int ret = imresize(src_buf, dst_buf);
    if (ret != IM_STATUS_SUCCESS) {
        fprintf(stderr, "imresize failed, ret=%d\n", ret);
        return -2;
    }

    return 0;
}
