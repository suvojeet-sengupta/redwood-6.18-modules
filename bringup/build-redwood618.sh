#!/bin/bash
# Build redwood's own out-of-tree drivers (redwood-6.18-modules/redwood) against
# the 6.18 kernel in ~/r618/out. Each module builds in ~/r618/vb/rw/<name>;
# logs land next to it. Usage: build-redwood618.sh [module ...]
R=${R:-$HOME/r618}; K=$R/redwood-6.18-kernel; O=$R/out; S=$R/redwood-6.18-modules/redwood; W=$R/vb/rw
export PATH=${CLANG:-$HOME/tc/clang-r596125/bin}:$PATH
MK="make -C $K O=$O ARCH=arm64 LLVM=1 LLVM_IAS=1 LOCALVERSION= ${PAHOLE:+PAHOLE=$PAHOLE} -j${JOBS:-$(nproc)}"
mkdir -p $W
mods=${*:-"xiaomitouch focaltech touch speaker btpower btfmslim fingerprint haptics ioncompat irspi mithermal subsys_compat usbrole adsprpc bootlog testguard logcap video cass"}
for m in $mods; do
	rm -rf $W/$m; cp -r $S/$m $W/$m
	E=""
	case $m in
	focaltech)
		cp $S/xiaomitouch/xiaomi_touch.h $K/include/linux/soc/qcom/panel_event_notifier.h $W/focaltech/ 2>/dev/null
		E="$W/xiaomitouch/Module.symvers" ;;
	touch)
		T=$K/drivers/input/touchscreen
		cp $T/goodix_berlin_core.c $T/goodix_berlin_spi.c $T/goodix_berlin.h $W/touch/
		python3 $S/touch/xm_touch.py $W/touch $S/xiaomitouch/xiaomi_touch.h \
			$K/include/linux/soc/qcom/panel_event_notifier.h > $W/touch-prep.log 2>&1
		E="$W/xiaomitouch/Module.symvers" ;;
	speaker)
		C=$K/sound/soc/codecs
		cp $C/aw88261.c $C/aw88261.h $W/speaker/; cp -r $C/aw88395 $W/speaker/
		sed -i 's/"aw88261_acf.bin"/"aw882xx_acf.bin"/' $W/speaker/aw88261.h
		python3 $S/speaker/xm_ctls.py $W/speaker > $W/speaker-prep.log 2>&1 ||
			{ cat $W/speaker-prep.log; echo "speaker: xm_ctls.py failed"; exit 1; }
		sed -e "/EXPORT_SYMBOL/d" -e "/MODULE_/d" $K/lib/crc/crc8.c > $W/speaker/crc8.c ;;
	btfmslim)
		E="$W/btpower/Module.symvers" ;;
	irspi)
		cp $K/drivers/media/rc/ir-spi.c $W/irspi/ ;;
	esac
	$MK M=$W/$m ${E:+KBUILD_EXTRA_SYMBOLS="$E"} modules > $W/$m.log 2>&1
	rc=$?
	echo "$m rc=$rc errors=$(grep -c 'error:' $W/$m.log) ko=$(ls $W/$m/*.ko 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
done
