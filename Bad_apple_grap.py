clear()
import Bad_apple_video
size = 32
dr = -1
fps = 15
start_time = get_time()
def gen_cadr():
	global map
	global dr
	global size
	global start_time
	start = dr +1
	x_size =[]
	x = 0
	for x in range(dr):
		move(North)
	cadr=0
	while True:
		if get_time() - start_time>=3:
			start_time= get_time()
			break
	move(West)
	while cadr < 3286:
		my_cadr = Bad_apple_video.video[cadr]
		my_cadr_shif = []
		
		for i in range(0, len(my_cadr), 2):
			count = my_cadr[i]
			value = my_cadr[i + 1] 
			for _ in range(count):
				my_cadr_shif.append(value)
		my_cadr_shif = my_cadr_shif[::-1]
				
		while True:
			if get_time() - start_time>=2.5:
				start_time= get_time()
				break
				
		for i in my_cadr_shif[dr*32:(dr+1)*32:]:
			if i == 0:
				if get_ground_type() == Grounds.Soil:
					till()
			else:
				if get_ground_type() != Grounds.Soil:
					till()
			move(West)
		cadr+=1
					
for x in range(max_drones()):
	dr += 1
	spawn_drone(gen_cadr)
gen_cadr()