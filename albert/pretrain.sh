pkill -f transformer-evolution
sleep 1

export PYTHONPATH=..
nohup python pretrain.py --epoch 100 --wandb True --config config.json >train.log 2>&1 &
