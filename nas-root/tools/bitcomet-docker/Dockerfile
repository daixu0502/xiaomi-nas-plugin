FROM wxhere/bitcomet-webui:v2.22.0
ARG NAS_UID=19718
ARG NAS_GID=19718
# Official image stores its program below /root. Permit traversal but do not
# grant the application write access to the program or the container root.
RUN chmod 0755 /root && mkdir -p /root/.config/BitComet /root/Downloads && \
    chown ${NAS_UID}:${NAS_GID} /root/.config/BitComet /root/Downloads
ENV HOME=/root TZ=Asia/Shanghai
USER ${NAS_UID}:${NAS_GID}
CMD ["/root/BitCometApp/usr/bin/bitcometd", "--webui_port", "8080", "--bt_port", "6082"]
